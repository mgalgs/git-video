"""Develop mode's performance: a changed file edited, on screen, from its old
text into its new one, the way a person at the keyboard would.

The planner's half is `build`: an edit script, a list of steps as plain data
(the plan's `steps`), in natural seconds:

- `move` -- point glides to (`line`, `col`), as C-n/C-f would take it;
- `jump` -- `M-g g N` typed in the echo area (`keys`, key `times`), point
  lands on `line` at `land`;
- `kill` -- point glides to `from` (over `glide`), the mark is set (`mark`),
  point glides to `to` and the region between is killed (`cut`); `text` is
  what goes;
- `type` -- `text` typed at (`line`, `col`), one key time per character in
  `times` (the leading whitespace of a new line comes with its RET, as
  electric indent puts it there); with `open` the trailing newline is put in
  first (C-o), so typing above a line does not drag it along;
- `fill` -- an elided run: `text` (whole lines) poured in after (`line`,
  `col`) over the step, behind a fold marker counting what is left.

Coordinates are 0-based lines and columns of the file with tabs expanded
(`split_lines`), every line ending with a newline; (`len(lines)`, 0) is the
end of the buffer. Each step's `at` and `dur` are seconds from the start of
the script; times inside a step are seconds from its `at`. `settle` lists
the lines a step leaves exactly as they are in the new file, as [row in the
step's region, new line index], so the film can colour them from the new
file; `lines` is the step's share of the lines done for the progress strip.
`apply` plays a script on a list of lines: the reference the tests hold the
film to.

The film's half is `Performance`: the buffer at any moment of a script, from
snapshots taken every SNAP_EVERY steps, so a frame costs the same anywhere in
the video; and `view`, where the window is.
"""
import bisect
import difflib
import math
from collections import Counter, namedtuple

from videobuilder.type.typewriter import schedule

from . import highlight, worddiff

# Natural timings, seconds: the pace of a person typing at ease.
PER_CHAR = 0.06           # a typed character, on average (schedule makes it uneven)
RET_REACH = 0.12          # the extra reach for RET
OPEN_LEAD = 0.15          # C-o, then the first character
GLIDE_MIN, GLIDE_MAX = 0.12, 0.4
MOVE_MIN, MOVE_MAX = 0.25, 0.7
MARK_GAP = 0.08           # glide landed -> C-SPC
SELECT_HOLD = 0.15        # the region held before C-w
AFTER_CUT = 0.12
AFTER_TYPE = 0.1
JUMP_KEYS = (0.0, 0.18)   # M-g, g
JUMP_DIGITS = 0.4         # the first digit of the line number
JUMP_RET = 0.15
SCROLL_NEAR, SCROLL_FAR = 0.45, 1.0
JUMP_HOLD = 0.15
FAR_ROWS = 52             # a jump farther than this scrolls with a ramp (plan.FAR_ROWS)
JUMP_ROWS = 26            # a hunk farther than this from point is jumped to (plan.PLAN_ROWS)
HUNK_PAUSE = 0.25         # a beat before each hunk
TAIL = 0.6                # the finished file held before the next shot
ELIDE_HEAD = 4            # an elided run types this many lines before it fills
FILL_PER_LINE, FILL_MIN, FILL_MAX = 0.03, 0.6, 2.5
FILL_POUR = 0.85          # a fill's lines are all in this far through it; the rest is a hold
PAIR_SHARE = 0.4          # an old and a new line sharing this much of their words are one line edited
SNAP_EVERY = 16
MARGIN = 3                # rows kept between point and the window's edge


def split_lines(text):
    """`text` as lines, tabs expanded, as highlight counts them: no line after a
    final newline; no lines at all for an empty file."""
    if not text:
        return []
    lines = text.expandtabs(highlight.TAB).split('\n')
    if text.endswith('\n'):
        lines.pop()
    return lines


# ---- the buffer as lines ----
def _splice(lines, a, b, s):
    """`lines` with the text from position `a` to `b` replaced by `s`, every line
    ending with a newline: (len(lines), 0) is the end of the buffer.
    Returns (new lines, l0, l1, pieces): the region was lines l0..l1 (l1 may
    be the end) and is now `pieces`."""
    (l0, c0), (l1, c1) = a, b
    n = len(lines)
    head = lines[l0][:c0] if l0 < n else ''
    tail = lines[l1][c1:] if l1 < n else ''
    pieces = (head + s + tail).split('\n')
    if l1 >= n and pieces[-1] == '':
        pieces.pop()
    return lines[:l0] + pieces + lines[l1 + 1:], l0, l1, pieces


def apply(lines, steps):
    """The lines after every step of a script (the reference)."""
    lines = list(lines)
    for st in steps:
        if st['kind'] == 'kill':
            lines = _splice(lines, st['from'], st['to'], '')[0]
        elif st['kind'] in ('type', 'fill'):
            p = (st['line'], st['col'])
            lines = _splice(lines, p, p, st['text'])[0]
    return lines


def _end_of(pos, text):
    """Where point is after `text` is inserted at `pos`."""
    l, c = pos
    k = text.count('\n')
    return (l + k, len(text) - text.rfind('\n') - 1) if k else (l, c + len(text))


# ---- the planner's half ----
def blocks(hunks):
    """[(hunk index, at, removed, added)] in file order: each run of `removed`
    old lines and the `added` new lines in their place, starting at new line
    index `at` (0-based). Pure additions have removed 0, pure deletions added 0."""
    out = []
    for hi, h in enumerate(hunks):
        covered = set()
        for at, old, n in h.segments:
            out.append((hi, at, len(old), n))
            covered.update(range(at + 1, at + n + 1))
        run = None
        for a, b in h.added:
            for ln in range(a, b + 1):
                if ln in covered:
                    continue
                if run and run[1] + run[3] == ln - 1:
                    run[3] += 1
                else:
                    run = [hi, ln - 1, 0, 1]
                    out.append(run)
    return sorted((tuple(x) for x in out), key=lambda x: (x[1], x[0]))


def _diff_blocks(old, new):
    """`blocks` from a line diff, when the hunks do not account for the texts."""
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return [(0, j1, i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != 'equal']


def pair_rows(old_rows, new_rows):
    """[(old row, new row)]: the lines of a replaced run that are one line
    edited (they share at least PAIR_SHARE of their words), in order, most
    shared words first (worddiff's equal tokens)."""
    ops = worddiff.ops(old_rows, new_rows)
    if not ops:
        return []
    shared = Counter((o[1], o[3]) for o in ops if o[0] == 'equal')

    def size(line):
        return sum(1 for m in worddiff.TOKEN.finditer(line) if m.group().strip())

    pairs = []
    for (i, j), c in sorted(shared.items(), key=lambda kv: (-kv[1], kv[0])):
        if c < max(1.0, PAIR_SHARE * max(size(old_rows[i]), size(new_rows[j]))):
            continue
        if all((pi - i) * (pj - j) > 0 for pi, pj in pairs):
            pairs.append((i, j))
    return sorted(pairs)


def _clamp(x, a, b):
    return max(a, min(b, x))


class _Script:
    """The builder: a live copy of the buffer, point, the clock and the steps."""

    def __init__(self, old, seed):
        self.cur = list(old)
        self.point = (0, 0)
        self.t = 0.0
        self.steps = []
        self.seed = seed
        self.written = {}   # new line index -> natural time it is finished

    def add(self, st, hunk):
        st['at'] = round(self.t, 4)
        st['dur'] = round(st['dur'], 4)
        st['hunk'] = hunk
        self.steps.append(st)
        self.t = st['at'] + st['dur']
        return st

    def glide(self, to):
        (l0, c0), (l1, c1) = self.point, to
        if (l0, c0) == (l1, c1):
            return 0.0
        return _clamp(GLIDE_MIN + 0.012 * abs(l1 - l0) + 0.004 * abs(c1 - c0), GLIDE_MIN, GLIDE_MAX)

    def keys(self, text, indent_first=False):
        """Key times for `text` from 0 (videobuilder's typewriter schedule, line
        by line) and their span. A new line's indentation arrives with its RET,
        as electric indent puts it there (the first line's too, with
        `indent_first`: a TAB on an opened line)."""
        times, t = [], 0.0
        for k, line in enumerate(text.split('\n')):
            if k:
                times.append(t)   # RET
                t += PER_CHAR + RET_REACH
            body = line.lstrip(' ') if k or indent_first else line
            times += [times[-1] if times else t] * (len(line) - len(body))
            ts, span = schedule(body, f'{self.seed}:{len(self.steps)}:{k}', PER_CHAR)
            times += [t + x for x in ts]
            t += span
        return times, t

    def move(self, to, hunk):
        d = abs(to[0] - self.point[0])
        self.add({'kind': 'move', 'dur': _clamp(MOVE_MIN + 0.015 * d, MOVE_MIN, MOVE_MAX) + 0.08,
                  'line': to[0], 'col': to[1]}, hunk)
        self.point = to

    def jump(self, line, hunk):
        digits = str(line + 1)
        ts, span = schedule(digits, f'{self.seed}:goto:{line}', PER_CHAR)
        times = [*JUMP_KEYS, *(JUMP_DIGITS + x for x in ts)]
        land = JUMP_DIGITS + span + JUMP_RET
        times.append(land)
        far = abs(line - self.point[0]) > FAR_ROWS
        scroll = SCROLL_FAR if far else SCROLL_NEAR
        self.add({'kind': 'jump', 'dur': land + scroll + JUMP_HOLD, 'line': line, 'col': 0,
                  'keys': f'M-g g {digits}', 'times': [round(x, 4) for x in times], 'land': round(land, 4),
                  'scroll': 'far' if far else 'near', 'scroll_dur': scroll}, hunk)
        self.point = (line, 0)

    def kill(self, a, b, hunk, weight, settle=(), keys=None):
        n = len(self.cur)
        a, b = tuple(a), tuple(b)
        text = '\n'.join(([self.cur[a[0]][a[1]:]] if a[0] < n else [''])
                         + self.cur[a[0] + 1:b[0]]) if a[0] != b[0] else self.cur[a[0]][a[1]:b[1]]
        if a[0] != b[0]:
            text += '\n' + (self.cur[b[0]][:b[1]] if b[0] < n else '')
        g = 0.0 if keys else self.glide(a)
        mark = g + MARK_GAP
        if a[0] == b[0]:
            sel = _clamp(0.05 + 0.025 * (b[1] - a[1]), 0.12, 0.5)
        else:
            sel = _clamp(0.15 + 0.03 * (b[0] - a[0]), 0.2, 0.7)
        cut = mark + sel + SELECT_HOLD
        st = {'kind': 'kill', 'dur': cut + AFTER_CUT, 'from': list(a), 'to': list(b), 'text': text,
              'glide': round(g, 4), 'mark': round(mark, 4), 'cut': round(cut, 4), 'lines': weight,
              'settle': [list(x) for x in settle]}
        if keys:
            st['keys'] = keys
        self.add(st, hunk)
        self.cur = _splice(self.cur, a, b, '')[0]
        self.point = a
        for _, j in settle:
            self.written[j] = self.t

    def type(self, pos, text, hunk, weight, settle=(), open_=False):
        g = self.glide(pos)
        body = text[:-1] if open_ else text
        times, span = self.keys(body, indent_first=open_)
        lead = g + (OPEN_LEAD if open_ else 0.0)
        times = [lead + x for x in times]
        if open_:
            times.append(g)   # C-o: the newline first
        st = {'kind': 'type', 'dur': lead + span + AFTER_TYPE, 'line': pos[0], 'col': pos[1], 'text': text,
              'times': [round(x, 4) for x in times], 'open': open_, 'glide': round(g, 4), 'lines': weight,
              'settle': [list(x) for x in settle]}
        self.add(st, hunk)
        self.cur = _splice(self.cur, pos, pos, text)[0]
        self.point = _end_of(pos, body)
        # Region row r is finished by the r-th newline typed (the first is the
        # one that opened it, unless C-o did), or at the end of the step.
        nl = [st['at'] + times[k] for k, ch in enumerate(body) if ch == '\n']
        for r, j in settle:
            self.written[j] = nl[r] if r < len(nl) else self.t

    def fill(self, pos, rows, hunk, settle):
        text = '\n' + '\n'.join(rows)
        dur = _clamp(FILL_PER_LINE * len(rows), FILL_MIN, FILL_MAX)
        self.add({'kind': 'fill', 'dur': dur, 'line': pos[0], 'col': pos[1], 'text': text, 'count': len(rows),
                  'lines': len(rows), 'settle': [list(x) for x in settle]}, hunk)
        self.cur = _splice(self.cur, pos, pos, text)[0]
        self.point = (pos[0] + len(rows), len(rows[-1]))
        for r, j in settle:
            self.written[j] = self.steps[-1]['at'] + dur * FILL_POUR * r / len(rows)

    # ---- what a block becomes ----
    def kill_lines(self, at, k, hunk):
        end = (at + k, 0)
        self.kill((at, 0), end, hunk, k)

    def type_lines(self, at, rows, js, hunk, elide):
        """Type whole new lines so they become lines at..at+len-1."""
        head = rows if not elide or len(rows) < elide else rows[:ELIDE_HEAD]
        if at > 0:
            pos = (at - 1, len(self.cur[at - 1]))
            self.type(pos, '\n' + '\n'.join(head), hunk, len(head),
                      settle=[(r + 1, j) for r, j in enumerate(js[:len(head)])])
        else:
            self.type((0, 0), '\n'.join(head) + '\n', hunk, len(head),
                      settle=list(enumerate(js[:len(head)])), open_=True)
        if len(head) < len(rows):
            last = at + len(head) - 1
            self.fill((last, len(self.cur[last])), rows[len(head):], hunk,
                      settle=[(r + 1, j) for r, j in enumerate(js[len(head):])])

    def edit_line(self, at, old, new, j, hunk):
        """One line edited in place: the changed words killed and the new ones
        typed, left to right."""
        ops = worddiff.line_edits(old, new)
        if not ops:
            self.written[j] = self.t
            return
        delta = 0
        for k, (col, dl, ins) in enumerate(ops):
            c = col + delta
            last = k == len(ops) - 1
            if dl:
                self.kill((at, c), (at, c + dl), hunk, 2 if last and not ins else 0,
                          settle=[(0, j)] if last and not ins else ())
            if ins:
                self.type((at, c), ins, hunk, 2 if last else 0, settle=[(0, j)] if last else ())
            delta += len(ins) - dl

    def block(self, hunk, at, k, new_rows, js, elide):
        old_rows = self.cur[at:at + k]
        pairs = pair_rows(old_rows, new_rows) if old_rows and new_rows else []
        i0 = j0 = 0
        line = at
        for i, j in pairs + [(len(old_rows), len(new_rows))]:
            if i > i0:
                self.kill_lines(line, i - i0, hunk)
            if j > j0:
                self.type_lines(line, new_rows[j0:j], js[j0:j], hunk, elide)
                line += j - j0
            if i < len(old_rows):
                self.edit_line(line, self.cur[line], new_rows[j], js[j], hunk)
                line += 1
            i0, j0 = i + 1, j + 1


def build(old, new, hunks, *, elide=None, seed=''):
    """The edit script that turns `old` lines into `new` (both from
    `split_lines`) hunk by hunk: {'steps', 'hunks' ([first, last] step index
    per hunk, or None), 'natural' (seconds), 'written' ({new line index:
    natural time it is finished})}. `elide`: a typed run of at least this many
    lines types ELIDE_HEAD of them and fills the rest."""
    found = blocks(hunks)
    for attempt in (found, None):
        bs = attempt if attempt is not None else _diff_blocks(old, new)
        sc = _Script(old, seed)
        if old and not new:   # the whole file goes: C-x h, C-w
            sc.kill((0, 0), (len(old), 0), 0, len(old), keys='C-x h')
        else:
            last_hunk = None
            for hi, at, k, n in bs:
                if hi != last_hunk:
                    sc.t += HUNK_PAUSE
                    if abs(at - sc.point[0]) > JUMP_ROWS:
                        sc.jump(min(at, max(0, len(sc.cur) - 1)), hi)
                    elif at != sc.point[0] and at < len(sc.cur):
                        sc.move((at, 0), hi)
                    last_hunk = hi
                sc.block(hi, at, k, new[at:at + n], list(range(at, at + n)), elide)
        if sc.cur == list(new):
            break
    sc.t += TAIL
    spans = {}
    for k, st in enumerate(sc.steps):
        a, _ = spans.get(st['hunk'], (k, k))
        spans[st['hunk']] = (a, k)
    return {'steps': sc.steps, 'hunks': [list(spans[h]) if h in spans else None for h in range(len(hunks) or 1)],
            'natural': round(sc.t, 4), 'written': sc.written}


def weight(steps):
    return sum(st.get('lines', 0) for st in steps)


# ---- clocks: natural seconds -> seconds on screen ----
def speed_ramp(t, times, speeds):
    from videobuilder.capabilities.principles.time_remapping import speed_ramp as ramp
    return ramp(t, times, speeds)


def source_time(t, clock):
    """The script's natural time at `t` s into its shot."""
    return speed_ramp(t, clock['times'], clock['speeds'])


def shot_time(src, clock):
    """The inverse of `source_time`: when natural time `src` is on screen
    (the speed is linear between keyframes, so each piece is a quadratic)."""
    times, speeds = clock['times'], clock['speeds']
    acc = speeds[0] * times[0]
    if src <= acc:
        return src / speeds[0]
    for (t0, s0), (t1, s1) in zip(zip(times, speeds), zip(times[1:], speeds[1:])):
        seg = (t1 - t0) * (s0 + s1) / 2
        if src <= acc + seg:
            need, k = src - acc, (s1 - s0) / (2 * (t1 - t0))
            if abs(k) < 1e-12:
                return t0 + need / s0
            return t0 + (-s0 + math.sqrt(s0 * s0 + 4 * k * need)) / (2 * k)
        acc += seg
    return times[-1] + (src - acc) / speeds[-1]


def speed_at(t, clock):
    """How many natural seconds play per second on screen at `t`."""
    from videobuilder.capabilities.principles.time_remapping import speed_at as at
    return at(t, clock['times'], clock['speeds'])


def clock(natural, s0, fast=None, ramp=0.4):
    """A shot's clock: speed `s0` throughout, or with `fast` = (natural start,
    natural end, peak) a speed-up from s0 to the peak and back, ramped over
    `ramp` s on screen each way. Returns ({'times', 'speeds'}, duration on
    screen, the fast span {'at', 'until', 'peak', 'times'} or None)."""
    if not fast:
        dur = natural / s0
        return {'times': [0.0, round(dur, 4)], 'speeds': [s0, s0]}, dur, None
    a_src, b_src, peak = fast
    a = a_src / s0
    plateau = (b_src - a_src - ramp * (s0 + peak)) / peak
    b = a + 2 * ramp + plateau
    dur = b + (natural - b_src) / s0
    times = [a, a + ramp, b - ramp, b]
    speeds = [s0, peak, peak, s0]
    if a > 0:
        times, speeds = [0.0] + times, [s0] + speeds
    if dur > b + 1e-6:
        times, speeds = times + [dur], speeds + [s0]
    return ({'times': [round(x, 6) for x in times], 'speeds': speeds}, dur,
            {'at': round(a, 4), 'until': round(b, 4), 'peak': round(peak, 3),
             'times': [round(a, 4), round(a + ramp, 4), round(b - ramp, 4), round(b, 4)]})


def hold(clk, span, dur, a_src, b_src, least, eps=1e-4):
    """The clock with natural [a_src, b_src] played over at least `least` s on
    screen, whatever speed was there (an elided run's fold must be seen to fill
    in, even inside a speed-up); the time it adds pushes the rest later.
    Returns (clock, the fast span moved with it, duration, seconds added)."""
    ta, tb = shot_time(a_src, clk), shot_time(b_src, clk)
    if tb - ta >= least - 1e-6:
        return clk, span, dur, 0.0
    extra = least - (tb - ta)
    sa, sb = speed_at(ta, clk), speed_at(tb, clk)
    # steps from the old speeds to v and back over eps: v is what makes the
    # area under the whole piece exactly b_src - a_src
    v = (b_src - a_src - eps * (sa + sb) / 2) / (least - eps)
    pairs = list(zip(clk['times'], clk['speeds']))
    times = [t for t, _ in pairs if t < ta - 1e-9] + [ta, ta + eps, ta + least - eps, ta + least]
    speeds = [x for t, x in pairs if t < ta - 1e-9] + [sa, v, v, sb]
    times += [t + extra for t, _ in pairs if t > tb + 1e-9]
    speeds += [x for t, x in pairs if t > tb + 1e-9]

    def moved(x):
        if x <= ta:
            return x
        if x >= tb:
            return x + extra
        return ta + (x - ta) * least / (tb - ta)

    if span:
        span = dict(span, at=round(moved(span['at']), 4), until=round(moved(span['until']), 4),
                    times=[round(moved(x), 4) for x in span['times']])
    return {'times': times, 'speeds': speeds}, span, dur + extra, extra


def ramp_fits(span_src, s0, peak, ramp):
    return span_src >= ramp * (s0 + peak)


# ---- the film's half ----
State = namedtuple('State', 'lines cursor region fold echo done removed since step')
# lines: ((origin, text), ...), origin ('old', i), ('new', j) or None (being edited);
# cursor (line, col); region ((l0, c0), (l1, c1)) or None; fold (line, count) or
# None: the marker row drawn under `line`; echo: the echo area's text; done: lines
# of the script's weight finished; removed: lines before which whole lines were
# killed; since: seconds since the last key (None while keys are going); step:
# the step running (-1 before the first).


def _shift(removed, l0, old_n, new_n):
    """Kill markers (lines before which whole lines went) after the `old_n`
    lines from l0 became `new_n`: they stay with the lines below them."""
    out = []
    for m in removed:
        if m <= l0:
            out.append(m)
        elif m >= l0 + old_n:
            out.append(m + new_n - old_n)
        else:
            out.append(l0 + max(0, new_n - 1))
    return out


class Performance:
    """A script played on a buffer: `state_at(t)` for any natural time t, from
    snapshots every `snap_every` steps (so at most that many steps are replayed
    for a frame), and `view(t, rows)`, where the window is."""

    def __init__(self, old, steps, snap_every=SNAP_EVERY):
        self.steps = steps
        self.starts = [st['at'] for st in steps]
        self.snap_every = snap_every
        self.replays = 0   # steps replayed by the last state_at (the tests bound it)
        lines = tuple((('old', i), s) for i, s in enumerate(old))
        state = (lines, (), (0, 0), 0.0)
        self.snaps = []
        self.before = []   # point before each step
        for k, st in enumerate(steps):
            if k % snap_every == 0:
                self.snaps.append(state)
            self.before.append(state[2])
            state = self._apply(state, st)
        self.final = state
        self._tops = {}

    # -- whole steps --
    @staticmethod
    def _splice(lines, a, b, s, settle, complete=None):
        """`_splice` on (origin, text) lines: a row of the region keeps its
        origin when its text is the line it came from, takes ('new', j) from
        `settle` (rows below `complete` only, when given), else is being edited."""
        texts = [x for _, x in lines]
        _, l0, l1, pieces = _splice(texts, a, b, s)
        n = len(lines)
        first = lines[l0] if l0 < n else None
        last = lines[l1] if l1 < n else None
        settled = {r: j for r, j in settle if complete is None or r < complete}
        region = []
        for r, x in enumerate(pieces):
            if r in settled:
                o = ('new', settled[r])
            elif r == 0 and first is not None and x == first[1]:
                o = first[0]
            elif r == len(pieces) - 1 and last is not None and x == last[1]:
                o = last[0]
            else:
                o = None
            region.append((o, x))
        out = lines[:l0] + tuple(region) + lines[l1 + 1:]
        return out, l0, l1, n - len(lines[:l0]) - len(lines[l1 + 1:]), len(region)

    def _apply(self, state, st):
        lines, removed, point, done = state
        kind = st['kind']
        if kind == 'kill':
            a, b = tuple(st['from']), tuple(st['to'])
            lines2, l0, l1, old_n, new_n = self._splice(lines, a, b, '', st['settle'])
            removed = _shift(removed, l0, old_n, new_n)
            if a[1] == 0 and b[1] == 0 and b[0] > a[0]:
                removed = sorted(set(removed) | {a[0]})
            return lines2, tuple(removed), a, done + st['lines']
        if kind in ('type', 'fill'):
            p = (st['line'], st['col'])
            lines2, l0, l1, old_n, new_n = self._splice(lines, p, p, st['text'], st['settle'])
            removed = _shift(removed, l0, old_n, new_n)
            body = st['text'][:-1] if st.get('open') else st['text']
            return lines2, tuple(removed), _end_of(p, body), done + st['lines']
        return lines, removed, (st['line'], st['col']), done

    # -- a moment --
    def state_at(self, t):
        k = bisect.bisect_right(self.starts, t) - 1
        if k < 0:
            lines, removed, point, done = self.snaps[0] if self.snaps else self.final
            return State(lines, point, None, None, '', done, removed, None, -1)
        base = k - k % self.snap_every
        state = self.snaps[base // self.snap_every]
        for st in self.steps[base:k]:
            state = self._apply(state, st)
        self.replays = k - base
        st = self.steps[k]
        tau = t - st['at']
        if tau >= st['dur']:
            lines, removed, point, done = self._apply(state, st)
            return State(lines, point, None, None, '', done, removed, tau - st['dur'] + self._idle(st), k)
        return self._partial(state, st, tau, k)

    @staticmethod
    def _idle(st):
        """Seconds from the step's last key to its end."""
        kind = st['kind']
        if kind == 'type':
            return st['dur'] - max(st['times'] or [0.0])
        if kind == 'kill':
            return st['dur'] - st['cut']
        if kind == 'jump':
            return st['dur'] - st['land']
        return 0.0

    def _partial(self, state, st, tau, k):
        lines, removed, point, done = state
        kind = st['kind']
        if kind == 'move':
            return State(lines, self._cursor_in(k, tau), None, None, '', done, removed, None, k)
        if kind == 'jump':
            times, land = st['times'], st['land']
            if tau >= land:
                return State(lines, (st['line'], st['col']), None, None, '', done, removed, tau - land, k)
            pressed = sum(1 for x in times[:-1] if x <= tau)
            digits = st['keys'].split()[-1]
            echo = '' if pressed == 0 else 'M-g-' if pressed == 1 else 'Goto line: ' + digits[:pressed - 2]
            return State(lines, point, None, None, echo, done, removed, None, k)
        if kind == 'kill':
            a, b = tuple(st['from']), tuple(st['to'])
            if tau >= st['cut']:
                lines, removed, point, done = self._apply(state, st)
                return State(lines, point, None, None, '', done, removed, tau - st['cut'], k)
            if st.get('keys'):   # C-x h: point at the top, the whole buffer marked
                cur, region = (0, 0), ((0, 0), b) if tau >= st['mark'] else None
                echo = 'Mark set' if region else 'C-x-' if tau >= st['mark'] - 0.06 else ''
                return State(lines, cur, region, None, echo, done, removed, None, k)
            g, mark, cut = st['glide'], st['mark'], st['cut']
            if tau < mark:
                cur = _lerp_pos(point, a, min(1.0, tau / g)) if g > 0 else a
                return State(lines, cur, None, None, '', done, removed, None, k)
            q = min(1.0, (tau - mark) / max(1e-6, cut - mark - SELECT_HOLD))
            cur = _lerp_pos(a, b, q)
            return State(lines, cur, (a, cur), None, 'Mark set', done, removed, None, k)
        if kind == 'type':
            p = (st['line'], st['col'])
            g = st['glide']
            if tau < g:
                return State(lines, _lerp_pos(point, p, tau / g), None, None, '', done, removed, None, k)
            text, times, opened = st['text'], st['times'], st.get('open')
            body = text[:-1] if opened else text
            typed = bisect.bisect_right(times[:-1] if opened else times, tau)
            ins = body[:typed]
            complete = ins.count('\n')   # region rows above the one being typed are finished
            if opened and tau >= times[-1]:
                ins += '\n'
            lines2, l0, l1, old_n, new_n = self._splice(lines, p, p, ins, st['settle'], complete=complete)
            removed = _shift(removed, l0, old_n, new_n)
            frac = typed / max(1, len(body))
            last_key = times[typed - 1] if typed else g
            return State(lines2, _end_of(p, body[:typed]), None, None, '', done + st['lines'] * frac,
                         tuple(removed), None if tau - last_key < 0.3 else tau - last_key, k)
        if kind == 'fill':
            rows = st['text'][1:].split('\n')
            m = _poured(len(rows), tau, st['dur'])
            p = (st['line'], st['col'])
            ins = '\n' + '\n'.join(rows[:m]) if m else ''
            lines2, l0, l1, old_n, new_n = self._splice(lines, p, p, ins, st['settle'], complete=m + 1)
            removed = _shift(removed, l0, old_n, new_n)
            cur = _end_of(p, ins)
            return State(lines2, cur, None, (cur[0], len(rows) - m), '', done + st['lines'] * m / len(rows),
                         tuple(removed), None, k)
        return State(lines, point, None, None, '', done, removed, None, k)

    # -- the window --
    def tops(self, rows):
        """The window's top line when each step starts, for a window of `rows`."""
        if rows not in self._tops:
            out, top = [], 0
            for k, st in enumerate(self.steps):
                out.append(top)
                top = self._top_in(k, st['dur'], rows, top)[1]
            out.append(top)
            self._tops[rows] = out
        return self._tops[rows]

    def _top_in(self, k, tau, rows, top):
        """(scroll, top) `tau` s into step k from window top `top`: scroll is
        (from, to, seconds into the scroll, its length, far) while a jump scrolls."""
        st, m = self.steps[k], min(MARGIN, max(0, (rows - 1) // 4))
        kind = st['kind']
        if kind == 'jump':
            if tau < st['land']:
                return None, top
            line = st['line']
            to = top if top + m <= line <= top + rows - 1 - m else max(0, line - rows // 3)
            into = tau - st['land']
            if to != top and into < st['scroll_dur']:
                return (top, to, into, st['scroll_dur'], st['scroll'] == 'far'), to
            return None, to
        if kind == 'kill' and st.get('keys'):
            return None, top
        cur = self._cursor_in(k, tau)
        if kind == 'kill':
            a = tuple(st['from'])
            top = _follow(top, a[0] if tau >= st['glide'] else cur[0], rows, m)
            if st['mark'] <= tau < st['cut']:
                top = _follow(top, cur[0], rows, m)
            elif tau >= st['cut']:
                top = _follow(_follow(top, st['to'][0], rows, m), a[0], rows, m)
            return None, top
        if kind in ('type', 'fill'):
            p = (st['line'], st['col'])
            if tau >= st.get('glide', 0.0):
                top = _follow(top, p[0], rows, m)
            return None, _follow(top, cur[0] + (1 if kind == 'fill' else 0), rows, m)
        return None, _follow(top, cur[0], rows, m)

    def _cursor_in(self, k, tau):
        st = self.steps[k]
        start = self.before[k]
        kind = st['kind']
        if kind == 'move':
            return _lerp_pos(start, (st['line'], st['col']), min(1.0, tau / max(1e-6, st['dur'] - 0.08)))
        if kind == 'kill':
            a, b = tuple(st['from']), tuple(st['to'])
            if tau < st['mark']:
                return _lerp_pos(start, a, min(1.0, tau / st['glide'])) if st['glide'] > 0 else a
            if tau < st['cut']:
                return _lerp_pos(a, b, min(1.0, (tau - st['mark']) / max(1e-6, st['cut'] - st['mark'] - SELECT_HOLD)))
            return a
        if kind == 'type':
            p = (st['line'], st['col'])
            if tau < st['glide']:
                return _lerp_pos(start, p, tau / st['glide'])
            body = st['text'][:-1] if st.get('open') else st['text']
            typed = bisect.bisect_right(st['times'][:-1] if st.get('open') else st['times'], tau)
            return _end_of(p, body[:typed])
        if kind == 'fill':
            rows = st['text'][1:].split('\n')
            m = _poured(len(rows), tau, st['dur'])
            p = (st['line'], st['col'])
            return _end_of(p, '\n' + '\n'.join(rows[:m]) if m else '')
        return start

    def view(self, t, rows):
        """(scroll, top) at natural time `t` for a window of `rows`: scroll as
        in `_top_in`, top the line the window starts at (where a scroll is
        going)."""
        k = bisect.bisect_right(self.starts, t) - 1
        if k < 0:
            return None, 0
        tops = self.tops(rows)
        st = self.steps[k]
        tau = t - st['at']
        if tau >= st['dur']:
            return None, tops[k + 1]
        return self._top_in(k, tau, rows, tops[k])


def _poured(n, tau, dur):
    """How many of a fill's n lines are in `tau` s into it."""
    return min(n, int(n * tau / (dur * FILL_POUR)))


def _follow(top, line, rows, m):
    """The window's top, moved as little as it must for `line` to stay `m` rows
    inside it (Emacs scrolling with a margin)."""
    if line < top + m:
        return max(0, line - m)
    if line > top + rows - 1 - m:
        return line - (rows - 1 - m)
    return top


def _lerp_pos(a, b, p):
    """Point part way from `a` to `b`: by lines first, then by columns, as
    C-n and C-f would take it."""
    p = max(0.0, min(1.0, p))
    (l0, c0), (l1, c1) = a, b
    if l0 == l1:
        return (l0, round(c0 + (c1 - c0) * p))
    dl = abs(l1 - l0)
    steps = dl + abs(c1 - c0) * 0.25
    q = p * steps
    if q <= dl:
        return (round(l0 + (l1 - l0) * q / dl), c0 if q < dl else c0)
    return (l1, round(c0 + (c1 - c0) * min(1.0, (q - dl) / max(1e-6, steps - dl))))

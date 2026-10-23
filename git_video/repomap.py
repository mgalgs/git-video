"""The repo map: every file at the target as a tile, sized by file size and
grouped by top-level directory, the changed files lit.

`tiles` is the planner's half: it picks which files get a tile of their own
and folds the rest into one "N more" tile per directory, so a big repository
still gives a legible map and every changed file always has its own tile.
The result is plain JSON and goes in the plan as `map`.
"""
import math

MAX_TILES = 36       # quiet (unchanged) tiles on the whole map, about
MAX_GROUPS = 10      # directories; the rest fold into one "k more directories" group
CHANGED_FLOOR = 0.06 # a changed tile's least share of the map's value ...
CHANGED_SHARE = 0.5  # ... lowered so the changed tiles' floors never pass this share together
QUIET_FLOOR = 0.012  # a quiet or folded tile's least share


def group_of(path, root):
    """(group label, label inside the group) of a path: grouped by the first
    directory; files at the top level go in a group named `root`."""
    head, sep, rest = path.partition('/')
    return (head, rest) if sep else (root, path)


def tiles(tree, files, root):
    """The map for the plan: `{'files': n, 'groups': [{'dir', 'tiles'}]}`.

    `tree` is [(path, size)] of every blob at the target; `files` the plan's
    file dicts (the changed files, in tour order). A changed tile is
    `{'path', 'size', 'added', 'deleted'}`, a quiet one `{'path', 'size'}`, a
    folded one `{'more': n, 'size': total}`. Groups holding a changed file
    come first, in the order of their first changed file, then by size."""
    sizes = dict(tree)
    changed = [f for f in files if f['path'] in sizes]
    order = {f['path']: k for k, f in enumerate(changed)}
    groups = {}
    for path, size in tree:
        groups.setdefault(group_of(path, root)[0], []).append((path, size))

    def rank(name):
        first = min((order[p] for p, _ in groups[name] if p in order), default=None)
        return (first is None, first or 0, -sum(s for _, s in groups[name]), name)

    names = sorted(groups, key=rank)
    lit = [n for n in names if not rank(n)[0]]
    rest = [n for n in names if n not in lit]
    room = max(0, MAX_GROUPS - len(lit))
    folded = []
    if len(rest) > room and len(rest) >= 2:   # one extra directory is fine; two or more fold
        rest, folded = rest[:max(0, room - 1)], rest[max(0, room - 1):]
        if len(folded) < 2:
            rest, folded = rest + folded, []
    kept = lit + rest

    # The quiet budget goes to the biggest quiet files across the kept groups.
    quiet = sorted(((s, p) for n in kept for p, s in groups[n] if p not in order), key=lambda x: (-x[0], x[1]))
    shown = {p for _, p in quiet[:max(0, MAX_TILES - len(changed))]}
    for n in kept:
        q = sorted(((s, p) for p, s in groups[n] if p not in order), key=lambda x: (-x[0], x[1]))
        left = [p for _, p in q if p not in shown]
        if len(left) == 1:   # a "1 more" tile says less than the file itself
            mine = [p for _, p in q if p in shown]
            if mine:
                shown.discard(mine[-1])
            else:
                shown.add(left[0])

    out = []
    for n in kept:
        entries = groups[n]
        ts = [{'path': f['path'], 'size': sizes[f['path']], 'added': f['added'], 'deleted': f['deleted']}
              for f in changed if group_of(f['path'], root)[0] == n]
        q = sorted(((s, p) for p, s in entries if p not in order), key=lambda x: (-x[0], x[1]))
        ts += [{'path': p, 'size': s} for s, p in q if p in shown]
        left = [(s, p) for s, p in q if p not in shown]
        if left:
            ts.append({'more': len(left), 'size': sum(s for s, _ in left)})
        out.append({'dir': n, 'tiles': ts})
    if folded:
        n = sum(len(groups[g]) for g in folded)
        out.append({'dir': f'{len(folded)} more directories',
                    'tiles': [{'more': n, 'size': sum(s for g in folded for _, s in groups[g])}]})
    return {'files': len(tree), 'groups': out}


def raw(tile):
    return math.sqrt(tile['size'] + 1)


def value(tile, total, changed=1):
    """A tile's area weight: the square root of its size (so a small changed
    file next to a big one still gets a readable tile, while bigger stays
    bigger), floored at a share of `total` (the sum of `raw` over the map).
    `changed` is how many changed tiles share the changed floor."""
    if 'added' in tile:
        floor = min(CHANGED_FLOOR, CHANGED_SHARE / max(1, changed))
    else:
        floor = QUIET_FLOOR
    return max(raw(tile), floor * total)


def key(tile, group):
    """A tile's name in marks and in the plan's `lit` list."""
    return tile['path'] if 'path' in tile else f'more:{group["dir"]}'

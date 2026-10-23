"""The tour's sound effects: keys while the minibuffer types, a mouse click on
each xref click, a soft tick as a hunk is reached, a whoosh on a buffer switch,
a softer whoosh for each camera move over the repo map and a low tick as each
changed tile lights.

Each sound is an id pair with a role fallback (the pattern of videobuilder's
terminal log), so a pack without the preferred ids still plays something
fitting, and no pack at all means a silent render with a one-line note.
"""
import math
import sys

import videobuilder as vb

from . import plan as planmod

# (preferred ids, fallback roles, gain dB, align)
KEY = (('pro_typing_key/tick', 'typing_key/crisp'), ('key', 'typing'), -10.0, 'onset')
CLICK = (('pro_click/mouse', 'click/soft'), ('click',), -6.0, 'onset')
TICK = (('pro_hover_tick/soft', 'hover_tick/detent'), ('tick',), -18.0, 'onset')
WHOOSH = (('whoosh_fast/tight',), ('whoosh',), -14.0, 'peak')
WHOOSH_SOFT = (('whoosh_soft/air', 'whoosh_fast/tight'), ('whoosh',), -22.0, 'peak')
TILE = (('hover_tick/detent', 'pro_hover_tick/soft'), ('tick',), -22.0, 'onset')


def pick(bank, ids, roles):
    """The first of `ids` the pack has, else the first sound of the first of
    `roles` it has, else None."""
    for i in ids:
        if i.split('--')[0] in bank.effects:
            return i
    have = set(bank.roles())
    for role in roles:
        if role in have:
            return bank.find(role)[0].id
    return None


def frame_at(start_frame, t, fps):
    """The first frame on which something `t` seconds into a shot shows."""
    return start_frame + math.ceil(t * fps - 1e-9)


def cues(shots, shot_frames, fps):
    """[(frame, sound name)] for a plan's shots: what should be heard, and when,
    whether or not a pack is there to play it."""
    out = []
    for s, f0 in zip(shots, shot_frames):
        kind = s['kind']
        if kind == 'map':
            lit = s.get('lit', [])
            out += [(frame_at(f0, planmod.light_start(k, len(lit)) + planmod.MAP_LIGHT_FADE / 2, fps), 'tile')
                    for k in range(min(len(lit), planmod.MAP_MAX_TICKS))]
        elif kind == 'dive':
            out.append((frame_at(f0, 0.35, fps), 'whoosh-soft'))
        elif kind == 'travel':
            out.append((frame_at(f0, 0.6, fps), 'whoosh-soft'))
        elif kind == 'open':
            keys, letters, ret, _ = planmod.open_timing(s['path'])
            out += [(frame_at(f0, t, fps), 'key') for t in (*keys, *letters, ret)]
            out.append((frame_at(f0, ret, fps), 'whoosh'))
        elif kind == 'hunk':
            out.append((frame_at(f0, planmod.HUNK_SCROLL, fps), 'tick'))
        elif kind == 'xref-push':
            out.append((frame_at(f0, planmod.XREF_CLICK, fps), 'click'))
            out += [(frame_at(f0, t, fps), 'key') for t in planmod.XREF_KEYS]
            out.append((frame_at(f0, planmod.XREF_SWITCH, fps), 'whoosh'))
        elif kind == 'xref-pop':
            out += [(frame_at(f0, t, fps), 'key') for t in planmod.POP_KEYS]
            out.append((frame_at(f0, planmod.POP_SWITCH, fps), 'whoosh'))
    return out


def sounds_for(shots, shot_frames, fps, log=None):
    """The `vb.sfx` sounds of a tour; () and one note on `log` without a pack."""
    try:
        bank = vb.sfx.bank()
    except vb.sfx.SfxUnavailable as e:
        print(f'git video: rendering without sound: {e}', file=log or sys.stderr)
        return ()
    table = {'key': KEY, 'click': CLICK, 'tick': TICK, 'whoosh': WHOOSH, 'whoosh-soft': WHOOSH_SOFT, 'tile': TILE}
    chosen = {name: (pick(bank, ids, roles), gain, align) for name, (ids, roles, gain, align) in table.items()}
    out = []
    for frame, name in cues(shots, shot_frames, fps):
        sid, gain, align = chosen[name]
        if sid is not None:
            out.append(vb.sfx.sound(sid, at=frame, fps=fps, gain_db=gain, align=align))
    return tuple(out)

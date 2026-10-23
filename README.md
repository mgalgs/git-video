# git-video

```
Usage: git video <rev|range> [options]

Create a video walk-through of a change set.

Options:

--mode     tour, develop
--series   Indicates that the patches in the range should be
           treated separated. Doesn't apply to all modes (tour
           ignores it).
--verbose=1,2,3
           1 means verbose. 3 means absolutely everything.
--voice[=<voice-profile>]
           Include voice over. Not reading the code, explaining
           the intent behind it. Like a good coding video online.
--format   16x9, 9x16, 1x1, all (default 16x9)
-o, --out  Output MP4 (default git-video-<short-sha>.mp4, or
           git-video-<a>-<b>.mp4 for a range; with --format all,
           -<format> goes before the extension).
--jobs N   Render workers (default: one per CPU).
--plan-json
           Print the shot plan as JSON and exit.
-C <path>  Run as if started in <path>, like git's own -C.
--font FAMILY
           Editor font family (default monospace).
--stills DIR
           Write a PNG of each key moment, plus sheet.png, to DIR
           and exit without rendering.

Modes:

tour    A solid xref session in $EDITOR (default=Emacs).
        Clicks through the relevant bits of code, highlighting things
        along the way, clicking into relevant definitions when
        interesting. Keyboard clicking noises. --verbose to control
        amount of side exploration.

develop   A pair programming session of the entire change set from top
          to bottom. Including rewrites when --series is provided (each
          patch builds on the last). Programming as a performance art
          where the code is the main character (credit: bisqwit).
          Can control elission and frequency of speed-up sequences with
          --verbose. Default is to aim for roughly 60s per 500 lines of
          code.

Keyboard clicking noises, mouse clicking and highlighting when something
should be highlighted. An xref visit results in a quick visual "click"
on the word being drilled down into. Use side sheets and bars for things
like call stack and overall orientation or supplemental information,
types, classes, definitions, semantic meaning behind large blocks (which
can be derived intrinsically and harvested aggressively from commit
messages), etc.

Uses mgalgs/videobuilder
```

## What it is

`git video` turns a commit (or a range, as one combined diff) into an MP4
that walks through the change the way a person would in their editor: it
opens each changed file, scrolls to each hunk and sweeps a highlighter over
the new lines, and now and then clicks an identifier and jumps to its
definition (`M-.`) and back (`M-,`). A side bar keeps the commit message,
the list of changed files and the xref stack in view.

The editor is not a recording of a real Emacs: it is drawn frame by frame
with [videobuilder](https://github.com/mgalgs/videobuilder), so a render is
deterministic and needs no display. It looks like Emacs: a dark theme, line
numbers, a fringe, a mode line and an echo area where the commands are
typed, with keyboard and mouse sounds.

A single rev `X` means the change `X^..X` (a root commit is shown against
the empty tree); `A..B` means the combined diff of the range.

## Install

```
pip install git+https://github.com/mgalgs/git-video
```

That puts `git-video` on your PATH, so git finds it as `git video`.
`python -m git_video` runs the same thing. Rendering needs `ffmpeg`.

Sounds come from videobuilder's sound-effects pack, fetched once with
`python -m videobuilder sfx fetch`. Without it the video renders silently
(you get a one-line note).

## What works today

- `--mode tour` on a commit or a range, in 16:9, 9:16 and 1:1.
- A map of the repository opens the tour: every file a tile sized by its
  size, grouped by top-level directory (small files folded into "N more"
  tiles), the changed files lit by how much changed. The camera dives from
  the map into the first file's tile, which grows into the editor, and
  between files pulls back out and travels to the next one.
- Syntax highlighting with Pygments.
- Xref without a language server: identifiers on the added lines are looked
  up with `git grep` against per-language definition patterns (Python,
  JavaScript/TypeScript, Go, Rust, C-like languages, shell, Lisps).
  `--verbose` sets how many definitions get visited.
- The xref is a dive: the pointer swells into a disc on the word, the click
  ripples, the definition opens out of the clicked word in a growing
  circle, and `M-,` shrinks it back into the word.
- An opening card: the repo name over the commit subject, `+a −d` rolling
  up on counters, the author on a name bar, and co-authors (from
  `Co-Authored-By:` trailers) as a row of initials. A range shows
  "N commits" and its span of dates.
- A closing card: a recap of the files visited and the definitions dived
  into, and the next commit's subject when the target has one on the
  current branch.
- Colour is never the only cue: added lines carry a `+` in the fringe,
  removed lines a `−` marker between lines, the current file in the
  side bar a `▶`, and a changed tile on the repo map an outline and its
  `+a −d` label.

## The plan seam

A tour is built in four steps: collect the git facts, plan a list of shots,
build a film from the plan, render it. The plan is plain JSON
(`--plan-json` prints it): a list of shots, each with a `scene` (`card`,
`editor` or `map`), a `kind` (`title`, `map`, `dive`, `travel`, `open`,
`hunk`, `xref-push`, `xref-pop`, `end`) and a duration, beside plan-level
facts: the repo `map` (its tiles, grouped by directory), the `commit`
(with its `coauthors` from `Co-Authored-By:` trailers), the `files`, the
`repo` name, and `next`, the commit after the target on the current branch
(or null). The planner is deterministic heuristics
for now; the JSON is the shape a later pass (an LLM, say) can rewrite.

## Planned

- `--mode develop`: the change typed in from top to bottom, with rewrites
  when `--series` is given.
- `--series`: each patch of a range on its own.
- `--voice`: a narration explaining the intent behind the change, voiced
  with videobuilder's `say` (Kokoro voice `bf_isabella` by default).
- More scenes beside the editor and the map: a call graph, explainer cards.

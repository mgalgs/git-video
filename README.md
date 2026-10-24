# git-video

```
Usage: git video <rev|range> [options]

Create a video walk-through of a change set.

Options:

--mode     tour, develop
--series   Indicates that the patches in the range should be
           treated separately: one tour or one performance per
           commit, each on top of the one before.
--verbose=1,2,3
           1 means verbose. 3 means absolutely everything.
--voice[=<voice-profile>]
           Include voice over. Not reading the code, explaining
           the intent behind it. Like a good coding video online.
           Spoken by Kokoro (default voice bf_isabella), with
           captions. Needs the voice extra,
           pip install 'git-video[voice]', and Kokoro's model files.
--narrator auto, fork-sandbox, heuristic
           With --voice, who writes the narration. fork-sandbox
           runs an agent session in a sandboxed clone that reads the
           change and explains its approach; heuristic works from
           the commit message. auto (default) uses fork-sandbox when
           it is on PATH, else heuristic.
--duration <time>
           Target length of the whole video, cards included:
           60, 60s, 1m30s. The planner keeps the hunks that matter
           most and lists the files it leaves out on the closing
           card. Never under 15s.
--format   16x9, 9x16, 1x1, all (default 16x9)
--quality  high, medium, small (default high): the encoder's
           quality, x264 CRF 18, 23 or 28. small is about a third
           the size of high.
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

`git video` turns a commit (or a range, as one combined diff, or with
`--series` commit by commit) into an MP4. `--mode tour` walks through the
change the way a person would in their editor: it
opens each changed file, scrolls to each hunk, shows what the hunk replaced
morphing into what it says now, and now and then clicks an identifier and
jumps to its definition (`M-.`) and back (`M-,`). The commit message's
sentences appear beside the code they explain. A side bar keeps the commit
message, the changed files, a call graph and the xref stack in view.

The editor is not a recording of a real Emacs: it is drawn frame by frame
with [videobuilder](https://github.com/mgalgs/videobuilder), so a render is
deterministic and needs no display. It looks like Emacs: a dark theme, line
numbers, a fringe, a mode line and an echo area where the commands are
typed, with keyboard and mouse sounds. Long lines wrap onto continuation
rows at word boundaries, as Emacs's `visual-line-mode` does (a word longer
than a row is the only one broken), with a curly arrow in the fringe and no
line number on the rows that carry a line on. In 16:9 the pane shows 90-odd
columns.

`--mode develop` performs it instead: every file starts from what it was
and is edited into what it is, on screen, as a person at the keyboard would
-- a pair programming session where the code is the main character.

A single rev `X` means the change `X^..X` (a root commit is shown against
the empty tree); `A..B` means the combined diff of the range, or with
`--series` each of its commits in turn.

## Install

```
pip install git+https://github.com/mgalgs/git-video
```

That puts `git-video` on your PATH, so git finds it as `git video`.
`python -m git_video` runs the same thing. Rendering needs `ffmpeg`.

`git video --help` opens the man page, `man/git-video.1` in this
repository. Copy or symlink it into a `man1` directory on your `MANPATH`:

```
mkdir -p ~/.local/share/man/man1
ln -s "$PWD/man/git-video.1" ~/.local/share/man/man1/
```

(`manpath` lists the directories `man` searches.) `python -m git_video
--help` prints the short help without it. The page is generated from the
block at the top of this README and the option help in `git_video/cli.py`
by `python man/mkman.py`; a test fails when it is out of date.

`--voice` needs the voice extra, which brings videobuilder's speech support
(kokoro-onnx), and Kokoro's model files:

```
pip install 'git-video[voice] @ git+https://github.com/mgalgs/git-video'
```

Without them `--voice` stops before rendering and says what to install and
where videobuilder looks for the model files (`$VIDEOBUILDER_KOKORO_DIR` or
`~/.cache/kokoro`).

Sounds come from videobuilder's sound-effects pack, fetched once with
`python -m videobuilder sfx fetch`. Without it the video renders silently
(you get a one-line note).

## What works today

- `--mode tour` and `--mode develop` on a commit or a range, with or
  without `--series`, in 16:9, 9:16 and 1:1.
- A map of the repository opens the tour: every file a tile sized by its
  size, grouped by top-level directory (small files folded into "N more"
  tiles), the changed files lit by how much changed. The camera dives from
  the map into the first file's tile, which grows into the editor, and
  between files pulls back out and travels to the next one.
- Syntax highlighting with Pygments.
- Animated diffs: a hunk that replaces lines opens on the old text, removed
  words struck through, then morphs into the new. Words are matched only
  within a line that was edited (one sharing most of its words with an old
  line): there, kept words stay put (or glide along their row when the line
  around them changes length), removed words dissolve and the changed words
  decode in briefly; a wholly new line wipes in, unscrambled. Added words
  stay underlined. Removed lines with nothing in their place appear as faint
  ghosts that dissolve and close up. Pure additions get a highlighter sweep,
  and so do hunks taller than a screenful.
- Focus: while a hunk is read the rest of the buffer dims (never fully), so
  the eye lands on the change.
- Commit-message callouts: the body's sentences (trailers stripped) are
  matched to the hunks they name (functions, files, identifiers, flags) and
  shown as a caption card beside the code with a line to the words (in
  develop mode in free space at least three rows from the typing when there
  is any, else the side bar's free area, else over code away from it; in
  9:16 and 1:1, where the side bar is a panel under the code, a card with no
  free space three rows from point, in either mode, sits over the commit
  text in that panel instead, its line still running to the words). For a
  range, each hunk uses the message of the commit that last touched it
  (`git blame` within the range). A beat of more than three commits (a
  range, or a `--series` beat of merged small commits) gets none: no one
  message speaks for its squashed diff. With `--voice`, the fork-sandbox
  narrator chooses the callouts instead (see below).
- A call graph in the side bar (16:9 and 9:16): the definitions the change
  touches and the ones the tour dives into, linked by the dives and by calls
  on changed lines, growing as the tour finds them; edges run in lanes
  between the names, never across one; each dive runs along its edge, the
  node on screen is a solid hub and the xref stack is numbered.
- A minimap at the editor's right edge: the whole file in miniature with
  `+`/`−` ticks on changed lines and a box that slides with the view.
- Long scrolls (more than two screens) speed up and slow down instead of
  crawling, the text leaning with the speed.
- Xref without a language server: identifiers on the added lines are looked
  up with `git grep` against per-language definition patterns (Python,
  JavaScript/TypeScript, Go, Rust, C-like languages, shell, Lisps). A name
  goes to the definition in the same class or file, else in a module the
  file imports it from, else the only one in the repository; still
  ambiguous, the tour does not dive.
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
- On every card a long subject wraps at spaces onto as many lines as the
  card has room for, then the type shrinks, and only then is it cut, with
  an ellipsis after a whole word: never inside a word or at a hyphen.
- `--voice`: a narration with captions, paced into the film (see below).
- `--duration <time>`: a target length for the whole video, whatever the
  size of the change (see below).
- `--quality high|medium|small`: the encoder's quality (x264 CRF 18, 23
  or 28), set on videobuilder's own render; `small` is about a third the
  size of the default `high`.
- Colour is never the only cue: added lines carry a `+` in the fringe,
  removed lines a `−` marker between lines, removed words a strike-through,
  added words an underline, the current file in the side bar a `▶`, the
  current call-graph node a solid, larger hub, and a changed tile on the
  repo map an outline and its `+a −d` label.

## --voice

A voice explains the change while the film plays, the way its author would
to a colleague watching the screen: what it does and why, not what the
editor shows. It is sparse: most shots pass in silence. Identifiers are
said as words (`draw_card` as "draw card", `film.py` as "film dot pie") and
captioned as written; nothing reads code, trailers, URLs or hashes aloud.

The heuristic narrator writes the script from the commit message: what the
change does (the subject as a sentence) and how big it is, the body's lead
sentence over the repo map, a body sentence that gives a reason over the
hunk it names, what a dive calls when the body names it, each patch of a
`--series`, and the close. A body sentence that only says what the code now
does is not read out.

The fork-sandbox narrator understands the change instead of quoting it.
git-video writes a brief (`git_video/narrator_brief.md`, filled in with
the commit messages, the limits and the plan's shots, each with an id) and
runs [fork-sandbox](https://github.com/mgalgs/fork-sandbox) on it, blocking:
an agent session in a sandboxed clone of the repository, checked out at the
change, reads the diff and the code around it and writes the script, each
phrase anchored to a shot id. It also chooses the callout cards: at most
two per hunk, each its own line of at most 12 words about why the hunk is
shaped the way it is. Its cards replace the commit sentences for the whole
video, and an empty list means no cards; an answer without the field keeps
the commit sentences, and a card on anything but a hunk is dropped with a
warning. It commits nothing, so the repository is left
as it was; its run dir is left in place and named at the end, so its work
can be read. `GIT_VIDEO_NARRATOR_MODEL` sets the model (default `opus`) and
`GIT_VIDEO_NARRATOR_ARGS` adds fork-sandbox options (shell-split), say
`--claude-credentials <file>`. The script is cached in
`$XDG_CACHE_HOME/git-video/narration/`, keyed by the range, the mode,
`--series`, `--verbose`, `--duration`, the model and the shots, so a second
render runs no session. Under `--narrator=auto` a session that fails, or writes nothing
usable, costs a one-line warning and the heuristic narrates instead.

Either way the script is held to the same limits: about one phrase per 8 s
of the unvoiced film plus the opening and closing, each at most 35 words,
and speech at most 45% of the voiced film; a script over them loses phrases
from its end (a fork-sandbox one that cannot be cut safely is rejected).

Shots stretch to the voice: a card or a hunk is held until its phrases and
a breath are done, and in develop mode a narrated hunk is typed no faster
than its phrase (down to natural pace), then point rests until the sentence
ends. Phrases never overlap and never run into the next shot.

With `--duration` the voice stays inside the target. A phrase is at most a
third of the target in words (12 to 35), the phrase count follows the
target's length, and the fork-sandbox brief says how long the video is and
lists only the shots kept. A narrated shot takes its time from the
unnarrated ones; if that is not enough to land within the target's
tolerance, phrases go from the end of the middle, never the opening or
the closing.

The voice is said by videobuilder's `say` (Kokoro, voice `bf_isabella` by
default) once per script, into `$XDG_CACHE_HOME/git-video/voice/`; a second
render of the same change runs no model. Nothing is written into the
repository. Captions follow the voice word by word, under the editor pane
in 16:9 and in the band below the code in 9:16 and 1:1; the sound effects
duck under the voice and typing goes quieter still.

## Develop mode

- Each file is edited from its old text (empty for a new file) into its new
  one, hunk by hunk: the lines to remove are marked (shaded and outlined)
  and killed, the new text typed in with an uneven, human cadence, point
  moving as a real one would. A line that keeps most of its words is edited
  in place -- the changed words killed, the new ones typed -- rather than
  retyped. A far hunk is reached with `M-g g N` in the echo area, the view
  scrolling there; a deleted file goes with `C-x h C-w`.
- Files go top to bottom: a file whose change writes a definition that
  another changed file's new lines use goes first, tests last, otherwise
  diff order. The camera pulls back to the repo map and dives into the next
  file (or `C-x C-f` for one the map does not show).
- Pacing: about 60 s per 500 changed lines (added + removed), plus the
  cards and camera moves. A change is never sped up to fit less than 15 s;
  one that types out in under 15 s at natural pace plays at natural pace,
  without padding. Typing runs up to four times its
  natural pace; past that, each file after its first moments is a speed-up
  sequence with a `▶▶ ×N` badge, ramped in and out, the keystrokes thinned to
  a typing run. A long new block (30 lines or more) is typed for a few lines,
  then pours in behind a fold marker (`… 37 lines`) counting down.
  `--verbose`: unset elides and speeds up freely; `1` elides only very long
  blocks (80 lines); `2` never elides but still speeds up; `3` types
  everything at natural speed, however long that takes.
- The side bar and stage are the tour's: the files (current one marked), the
  commit, the call graph growing as definitions are written, the minimap
  filling in. The commit sentence that explains a hunk sits beside point
  while it is written, and the mode line counts the lines done.

## --series

Every commit of the range plays in order on top of the one before, so a
later patch visibly rewrites what an earlier one wrote (in develop mode) or
is toured on its own (in tour mode). Between two patches the next commit's
card comes to the top of a deck, over a strip of the series with the
current patch marked, and the side bar follows the patch. One opening and
one closing card frame the whole. A single rev is the same with or without
`--series`.

With `--duration`, the target is shared among the patches by their size,
with a floor so each keeps its card and at least one hunk. A series too long
for the floors plays fewer beats: the smallest adjacent patches merge into
one, whose card says so ("3 small commits").

## --duration

`--duration 60` makes a video of any change set, one small commit or a
whole release, run about 60 seconds (`60s` and `1m30s` work too), cards
included: within 10% of the target, or 3 s under 30 s. It still tells the
story of the change rather than playing its first minute: condensing is
selection first, then speed.

- Every hunk is scored: the size of the change, new definitions,
  definitions other changed code calls (through the xref index; the more
  callers the higher, on a log scale), hunks the commit messages talk about
  (the callout matcher), new files over edits, source over docs, fixtures
  and tests, and a top-level README (above all when the change adds it:
  it says what the project is).
- The cards and the map shrink first, to floors that keep them readable.
  The highest-scoring hunks that fit what is left are kept (a hunk less
  than half as important as one that did not fit does not take its place),
  and shown in story order (the tour's file order, develop's top-to-bottom order).
  Files with nothing kept are not visited: they stay lit on the map, the
  opening card's totals still count the whole change, and the closing card
  lists them under ALSO CHANGED with their `+`/`−` counts, the most
  important first. A release leans
  on the map: the camera dives into its few most important files.
- Only then is every shot sized, by one factor within its limits, to land
  on the target: kept hunks keep their natural read when they fit at it,
  and the camera's dives and travels play their moves faster when they are
  shortened. No xref dive under 30 s, at most one under 60 s.
- Develop types only the kept hunks, at the speed the target needs (up to
  ×256 under `--duration`); the rest of a visited file is put in at once,
  one step per run of hunks, under a `⇥ N hunks applied` marker (only the
  changed lines are marked as changed).
- With `--series`, each patch gets a share of the target by its size, and
  a long series merges its smallest neighbouring patches into one beat
  (see above).
- A target under 15 s is an error that says so. A change too small to fill
  the target stays shorter (its shots stretch only so far), with a note on
  stderr. Rendering prints what was kept and why, one line per hunk, and
  each file left out, to stderr.

## The plan seam

A tour is built in four steps: collect the git facts, plan a list of shots,
build a film from the plan, render it. The plan is plain JSON
(`--plan-json` prints it): a list of shots, each with a `scene` (`card`,
`editor` or `map`), a `kind` (`title`, `map`, `dive`, `travel`, `open`,
`hunk`, `xref-push`, `xref-pop`, `edit`, `patch`, `end`), the `patch` it
belongs to and a duration, beside plan-level
facts: the repo `map` (its tiles, grouped by directory), the `commit`
(with its `coauthors` from `Co-Authored-By:` trailers and, per commit, a
trailer-free `body`), the `files` (each with its `marks`: added line ranges
and where lines were removed), the `repo` name, `next`, the commit after the
target on the current branch (or null), and the call `graph` (`nodes` and
`edges`, each with the `first` shot that discovers it).

A `hunk` shot carries `focus` (its changed line span), `scroll` (`near` or
`far`), `morph` (null, or when it starts, how long it takes and its
`segments`: the old lines, how many new lines replace them and word-level
`ops`), `callouts` (the commit sentences shown, or the narrator's cards,
with their times and the words they point at) and `node` (its call-graph node). An `xref-push` shot
carries its `node` and `edge`.

The plan also says its `mode` and lists its `patches` (one without
`--series`): each with its `commit`, `files` and, for develop, the `lines`
performed, the `budget`, and the `natural` and played (`perf`) seconds;
with `--duration`, its content `target` in seconds, how many `hunks` it
chose from, the hunks `kept` (per file, each with its `index`, `score` and
`why`) and the files `left_out`, and the plan says its `duration`. A
develop `edit` shot carries its file's edit script as `steps` (`move`,
`jump`, `kill`, `type`, `fill`, and with `--duration` `apply`, each with
its times in natural seconds),
the `clock` that maps the shot's seconds to the script's (speed keyframes
for `time-remapping`'s `speed_ramp`), its `fast` spans and `elisions` in
seconds on screen, its `callouts`, and where it stands in the patch's lines
(`done_before`, `lines`, `total_lines`). The film only plays it: the buffer
at any frame is a function of the frame, from snapshots of the script.

With `--voice` the plan also carries its `voice`: the Kokoro `voice` and
`speed`, the cache `dir` its clips are in, the `lexicon` of spoken forms,
and its `phrases`, each with its `text` (as captioned), the `shot` it is said
over, its `start` and `end` on the film's clock in seconds, and its `audio`
clip. Without `--voice` the plan has no `voice` key and is exactly what it
was.

The planner is deterministic heuristics for now; the JSON is the shape a
later pass (an LLM, say) can rewrite.

## Planned

- `--voice` written by a model from the change rather than by heuristics.
- More scenes beside the editor and the map: explainer cards.

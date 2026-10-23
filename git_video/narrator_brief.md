# Write the narration for a video walk-through of a code change

You are writing the voice-over for a short video that walks a viewer
through a change to this repository. The video is already planned: an
editor pane shows each changed file, scrolls to each hunk, and sometimes
dives into a definition. Your words play over it. Do not change or
commit anything in this repository. Your only output is one file:
`outbox/narration.json`.

## The change

{range}

Commit messages, in order:

{commits}

Inspect it yourself: `git diff {base}..{tip}` (or `git show` per commit),
and read the surrounding code, the callers, and anything else that helps
you understand *why* the change is shaped the way it is.

## What to say

Narrate the way a senior engineer explains their change to a colleague
who is watching the screen: the problem, the approach, and the reasons.
Say what the change introduces, which pieces had to change to make room
for it, how they now fit together, and any trade-off or non-obvious
decision behind it. For example: "This change introduces a call graph to
the sidebar. To feed it, the sidebar is threaded through the planner,
which gains a way to record which definitions each shot discovers."

Do not describe what the editor is doing, and do not list files or
functions in order ("First, film.py." "A change to draw."). The viewer
can see that. Your words add the meaning the picture cannot show.

Be sparse. Silence is good. Most shots should have no phrase at all.
Speak when a shot shows something your explanation is about, so that
words and picture line up. Aim for an opening of one or two sentences
that frames the whole change, a few phrases at the moments that carry
the approach, and a short closing. The limits are hard: at most
{max_phrases} phrases, each at most 35 words, and about {max_speech_s}
seconds of speech in total (about 2.5 words per second).

Write for the ear: short sentences, plain words. Name code only when the
name helps, and say it as a person would ("the draw function"). Never
read out hashes, paths with slashes, punctuation, or code.

## The shots

Each line is a shot id, its start time and length in seconds (before
narration stretches anything), what it shows, and the file and lines.

```
{shot_table}
```

## Output

Write `outbox/narration.json`, in your artifact outbox (not in the
repository):

```json
{"phrases": [{"shot": "<shot id>", "text": "<what to say>"}]}
```

Phrases must be in shot order, with at most two per shot. The first
phrase goes on the opening shot, and the last may go on the closing
shot. Write the file, check that it parses as JSON, and end your turn.

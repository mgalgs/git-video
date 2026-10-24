# Write the narration for a video walk-through of a code change

You are writing the voice-over for a short video that walks a viewer
through a change to this repository. The video is already planned: an
editor pane shows each changed file, scrolls to each hunk, and sometimes
dives into a definition. Your words play over it. Do not change or
commit anything in this repository. {output}

## The change

{range}

Commit messages, in order:

{commits}

{inspect}

## Find the story first

The diff says what changed. The viewer needs to know where it sits in
the system and why it matters there, and the diff alone cannot tell you
that. Before you write a word, place each changed piece in the system:

- **Up the call stack.** Who calls the changed code, and who calls
  them? Follow the callers up until you reach an entry point: a command
  the user runs, a request handler, a main loop, a scheduled job. That
  path is how the change reaches anyone. Note what the user is doing
  when this code runs.
- **Down the call stack.** What does the changed code call, and what do
  those calls rest on? Follow them down through this repository's own
  layers to the libraries, the operating system or the hardware
  underneath: a subprocess, a file, the network, a renderer, an encoder.
  Few calls are more than a dozen steps from bare metal. Knowing the
  bottom tells you what the code is really for.
- **Beside it.** What else sits at the same level and does a similar
  job? Does the change follow its siblings, or break from them, and why?
- **The larger system.** What is this repository, and what is it part
  of? For example: a command-line tool, a library that others build on,
  a service with clients, a plugin to something bigger. Say what role
  the changed component plays in that whole.

Use your tools to read and search the code; the shot table names the
definitions each hunk is in and uses. You do not need to trace every
line. Trace until you can tell the story.

## What to say

Tell that story the way a senior engineer explains a change to a
colleague who is watching the screen. Start with the system and the
problem: what this part of the system does, and what was missing, wrong
or slow. Then explain the approach, and why the change lands where it
does: which layer owns the problem, what the callers above now get, and
what it builds on below. Name any trade-off or non-obvious decision.
End with what the system can do now that it could not do before.

For example: "When you run git video, the planner decides every shot
before a single frame is drawn. This change gives the planner a call
graph, so a shot can show who calls the function on screen. The sidebar
draws it, and it is fed the same plan the film reads, so the two never
disagree."

Every phrase should add something the picture cannot show: the reason,
the context, the consequence. Do not describe what the editor is doing,
and do not list files or functions in order ("First, film.py." "A
change to draw."). The viewer can see that.

Leave silence where the picture carries itself. Speak when a shot shows
the part of the story you are telling, so that words and picture line
up. Context from elsewhere in the system, such as a caller far up the
stack, is said over the shot of the code it explains.

A phrase goes on the shot that shows the thing it talks about. A phrase
about how the side bar is drawn goes on the hunk that changes the
drawing, not on the tests that check it. The opening and the closing
frame the whole change and are exempt. A phrase that names a definition
or a file shown by another shot, but not by its own, is moved to the
nearest shot that shows it when that keeps the order.

Aim for an opening of one or two sentences that sets the system and the
problem, phrases at the moments that carry the story, and a closing
that says what is now possible. The limits are hard: at most
{max_phrases} phrases, each at most {max_words} words, and about {max_speech_s}
seconds of speech in total (about 2.5 words per second). The finished
video is about {length} long, narration included, and the shot table
below is everything it shows: fit your explanation to that.

Write for the ear: short sentences, plain words. Name code only when the
name helps, and say it as a person would ("the draw function"). Never
read out hashes, paths with slashes, punctuation, or code.

## The shots

Each line is a shot id, its start time and length in seconds (before
narration stretches anything), what it shows, and the file and lines.
A hunk says the definitions it is in ("in draw, layout") and the ones
another shot shows that it uses ("uses build").

```
{shot_table}
```

## Callouts

A hunk shot (or an edit shot's hunk, an id like `s4h1`) can show
callout cards: a short line on screen beside the code. You choose them.
Give a hunk at most {max_callouts} cards, each your own line of at most
{callout_words} words about why that hunk is shaped the way it is. A card
may quote the commit message. It never describes the editor or what the
code visibly does. The shot table shows the cards the commit-message
matcher would pick, as a hint only. Most hunks need no card, and none is
better than noise: `"callouts": []` shows no cards at all. Leaving the
`callouts` key out keeps the matcher's cards.

## Output

{output_how}

```json
{"phrases": [{"shot": "<shot id>", "text": "<what to say>"}],
 "callouts": [{"shot": "<hunk shot id>", "text": "<card text>"}]}
```

Phrases must be in shot order, with at most two per shot. The first
phrase goes on the opening shot, and the last may go on the closing
shot. {output_end}

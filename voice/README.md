# voice

Two things live here:

- **Agent status** (`status.py`, `events.py`, `hooks/`), which every part of
  Radio Room uses, the desktop window included. See
  [../docs/status.md](../docs/status.md).
- **The voice server** (`server.py`, the optional `voice` feature): a
  phone-friendly web UI for keeping tabs on the coding agents (Claude Code /
  Antigravity / Copilot CLI) running in per-project tmux sessions, and for
  talking them over with a separate **Sonnet advisor** using voice dictation.
  The rest of this README is about it.

It needs the `claude` CLI for the advisor; the rest of Radio Room doesn't.

## The page

The server always serves the page, whether or not the voice feature is on. A
tab bar along the bottom mirrors the desktop window:

| tab | what | needs |
|---|---|---|
| **Agents** | one card per agent, most recently changed first, and a *New* section for projects added lately; the badge counts the ones waiting on you | — |
| **Projects** | the registry as an accordion — a repository shared by several projects (a monorepo's worktrees) under its own heading; each project's branch, worktree, subdirectory and tickets. One open at a time: a toggle for which agent, then Start — or **Terminal** (full screen, below), Talk and Stop | — |
| **Tickets** | your Jira tickets (the config's JQL) by workflow stage, the projects linked to each, the description and last comments on More, and Open in Jira ↗; read-only — changes are the desktop's and `proj jira`'s | `[jira]` |
| **Talk** | the advisor and the operator: everything from [Agent instances](#agent-instances) down | feature `voice` |

**Live terminals.** Terminal opens the agent's session full screen — a
terminal squeezed into a card is no use — with a bar for going back (‹, or
the phone's back gesture) and switching agent. It is sized to the visible
viewport, so the soft keyboard shrinks it rather than covering it. Under it
is xterm.js (terminal/'s vendored copy, served at `/vendor/`) over a
WebSocket, `/ws/attach?project=&agent=&cols=&rows=`, to a PTY running `tmux
attach` on that session (or `docker exec -it … tmux attach` for a container)
— `live.py`. It is another tmux client, like the desktop window's tabs:
going back or switching agent detaches it and the agent keeps running, and a
dropped connection reattaches, which tmux answers with a full repaint. On a
phone a key row adds esc, tab, a sticky ctrl, arrows, paste and ⏎, and a drag
scrolls (the app's own scrolling where it took the mouse). An agent card's
**Terminal** opens the same view. tmux sizes a session's window to its
most recently active client, so typing on the phone narrows it on the desk
until the desk is typed in again.

**Images.** The agent can't see the phone's clipboard or photos, so the key
row's **image** key (photos, camera, files), the paste key when the clipboard
holds an image, a browser paste or a drop on the terminal uploads it to
`POST /api/image?project=&agent=`. `core/pastes.py` saves it under
`~/.cache/radio-room/pastes/` (kept a week), or copies it into the agent's
container, and the page pastes its path into the terminal. Claude Code turns a
pasted image path into an attached image, as Ctrl+V does at the desk. A
format Claude can't read (an iPhone's HEIC) is redrawn as a JPEG first.

With the voice feature off there is no Talk tab, no ⚙, no music or speech,
and the server refuses the advisor's endpoints — what's left is a way to
check on agents and tickets from a phone. `#home`, `#repos`, `#tickets` and
`#talk` open a tab directly.

The project list comes from the registry (`core/registry.py`); nothing about
the existing tmux sessions changes. Settings are the `[voice]` and `[status]`
tables of Radio Room's config file (`config.example.toml` at the repo root).
A project can run two agents side by side — see
[Agent instances](#agent-instances).

## Agent instances

`proj agent` starts either **Claude Code** or **Antigravity** (`agy`) for a
project, or both: Claude in the project's base tmux session (`render`), agy
beside it (`render-agy`), or `agy` inside a project's docker container. Each
of those is an *instance*, and everything here — the terminal panel, Send, the
advisor's tools, the reply watcher — acts on one instance at a time.

Which tmux session belongs to which agent is the registry's rule, so it isn't
copied here: status.py asks `registry.agent_target()`, and the agents offered
are `registry.AGENTS`.

- **Choosing.** When a project has a second agent running, a `Claude | Agy`
  switch appears beside the conversation switch above the composer. The
  placeholder names the choice ("Ask about garmin · Agy…"). It's remembered per
  project; never chosen, it is whichever is running, Claude Code first.
- **One conversation per project.** Switching agents doesn't start a new advisor
  chat. Each turn tells the advisor which agent you're focused on and attaches
  that terminal, and mentions the other one if it's running, so "what's agy
  doing?" works. Its tools take an optional `agent` and otherwise act on your
  focus. Sent prompts and summaries are tagged with the agent they went to.
- **Operator.** Every running instance of a selected project is attached,
  labelled by agent; the cap (`operator_max_panes`) counts terminals, not
  projects.
- **Models.** Antigravity's `/model` takes ids like `gemini-4-argon-high` (see
  `agy models`), and — unlike Claude Code — saves the pick as the default for
  every new agy session. The advisor's `set_agent_model` tool says so when it
  uses it there.

## What it does

- **Status** — one tap; Sonnet reads a fresh capture of the project's tmux pane
  and summarizes what the agent is doing / whether it needs you.
- **Brainstorm / discuss** — dictate into the composer (iPhone keyboard mic)
  and chat with Sonnet about the session. Sonnet also has read-only access
  (Read/Glob/Grep) to the project directory, so it can check actual files.
- **Send to project** — flip the composer's destination to **Project** and the
  same text is pasted into the tmux session and submitted (with a confirm step).
  If Sonnet drafts a prompt in a code block, tap **"Use as prompt"** to load it
  into the composer.
- **Send by voice** — or just say "send it" / "tell it to run the tests" and the
  advisor submits the prompt itself, through the bundled MCP server. No taps,
  no confirm dialog. What it sent shows up as a **➤ sent to project** bubble.
- **Agent reply summaries** — after a prompt goes to the agent, the server
  watches its pane until it stops changing, then the advisor sums up what it
  did in a sentence or two. That summary appears as its own bubble (and is read
  aloud if 🔊 Speak is on) instead of dropping you into raw terminal output.
  It's a turn on the same advisor conversation, so "which of those two options
  would you pick?" just works.
- **Multi-project mode** — a projects view with an operator that watches every
  selected agent at once, reports across them, and hands you into any one of
  them for actual control. See [Multi-project mode](#multi-project-mode).
- **Terminal panel** — collapsible live view of the tmux pane (refreshes every
  4 s while open). Still there when you want the unfiltered version.
- **Speech speed** — 0.8× / 1× / 1.2× buttons in the bar under the header set
  how fast replies are read aloud. The setting lives on the server
  (`tts_rate` in `state.json`, via `POST /api/tts/rate`), defaults to 1.2×, and
  a change applies from the next sentence — including mid-reply. You can also
  just ask the operator ("talk slower", "back to normal speed"): its
  `set_speech_speed` tool sets the same slow / medium / fast value, every chat
  reply carries the current rate, and the page applies it before speaking —
  so the reply that confirms the change is already read at the new speed.
- **Music** — with the option on (⚙, off by default), the same bar streams the
  [musicforprogramming.net](https://musicforprogramming.net) mixes while you
  work. It keeps playing while the agent does, and pauses itself the moment a
  summary lands so the spoken update comes through clean. ▶ resumes, and
  tapping the episode title jumps to another mix.
- **Voice conversation** — tap the mic for push-to-talk speech input, toggle
  **🔊 Speak** to have replies read aloud, or toggle **🎙 Convo** for a full
  hands-free loop: listen → send → spoken reply → listen again. Voice turns
  use Haiku for low latency; typed brainstorming uses Sonnet (see config).
  Speech in and out can come from the browser or from local servers on the
  machine running this — ⚙ in the header picks, per browser. See [Speech](#speech).

The advisor runs headless via `claude -p`, so it uses your existing Claude
Code auth — no API key setup. Conversations persist per project
(`state.json`), surviving server restarts.

## Agent status

The states on the cards — hooks, the pane monitor, local summaries, usage —
are shared with the desktop window and documented in
[../docs/status.md](../docs/status.md). This server runs the monitor when it
holds the machine's monitor lock, and serves the result:

`GET /api/instances` (every running agent, plus dead ones still holding
something unseen and new projects, most recently changed first), `GET /api/event?id=` (one event in
full), `POST /api/instances/dismiss {project, agent}`, `POST /api/event/sonnet
{id}` (a better summary line from Sonnet, on request), `GET /api/usage` and
`POST /api/usage/refresh` (quotas and token rollups, with the `usage`
feature).

## The home screen

The page opens on **Agents**: one card per running agent instance, in three
groups — **Needs you** (waiting on a question, a menu or a permission prompt;
finished; failed), **Working**, and a collapsed **Idle** list of one-line rows.

    NEEDS YOU · 3
    ┃ needs you  render  Claude                       41m
    ┃ ⚙ 5 in background
    ┃ ROUGH SUMMARY · GRANITE
    ┃ Agent asks whether to commit this round with the phone controls included.
    ┃ [✓ Seen] [🎤 Talk] [Terminal] [More]

Each card has the agent, its state and how long it has been in it, projects
sharing the session ("also music-lab"), background shells still running, and
the turn's rough summary — or "last turn" while the agent is working again.
Where Granite's reading of the turn disagrees with the state, it says so in a
line under the summary; the card stays where the rules put it.

- **✓ Seen** clears a finished or waiting card back to idle.
- **🎤 Talk** opens that project's conversation with that agent selected
  (with the voice feature).
- **Terminal** opens that agent's live terminal, full screen.
- **More** shows the full final message, the transcript path, the live
  terminal, and *Ask Sonnet for a summary*.

A reload returns to the tab you were on, or the project you were talking
to. There is no composer on the list tabs, so turning to one ends a
hands-free conversation.

## The MCP server

`mcp_server.py` is a stdio MCP server (stdlib only, no SDK) that `server.py`
starts for each advisor turn via `claude -p --mcp-config ... --strict-mcp-config`.
Which tools it publishes depends on who is running.

A **project advisor**, scoped to the project you have open:

| tool | what it does |
| --- | --- |
| `send_prompt(prompt)` | types the prompt into the project's tmux session and presses Enter |
| `read_terminal(lines)` | fresh capture of the pane, for checking whether a send landed |
| `clear_agent_context()` | submits `/clear` to the agent, wiping the context it carries |
| `set_agent_model(model)` | submits `/model <name>`, changing what the agent codes with |
| `open_projects_view()` | sends the screen back to the multi-project overview |

The **operator**, which spans projects and dispatches to none of them:

| tool | what it does |
| --- | --- |
| `read_terminal(project, lines)` | capture any project's pane, selected or not |
| `select_projects(projects, mode)` | set the working set — `only`, `add` or `remove` |
| `open_project(project)` | hand the screen to one project and its advisor |
| `start_agent(project, agent)` | start an agent session, the launcher's way (`registry.start_agent`) |
| `stop_agent(project, agent)` | kill an agent session (`registry.stop_agent`) |
| `start_web(project)` / `stop_web(project)` | bring a web app up or down, the hub's way |
| `set_speech_speed(speed)` | how fast replies are spoken — `slow`, `medium` or `fast`, shared with the speed buttons |

The lifecycle tools go through `POST /api/control`, which calls the
registry's own functions, so a session started by voice is the one the
launcher would have started and the hub sees the same web app. The operator
is told to stop only what was named, and to ask first if that agent is
mid-turn. Its roster says, per project, which agents are running and whether
the web app is up.

`server.py` decides both lists: it passes the names in `VP_TOOLS`, and the MCP
server publishes exactly those, so `--allowedTools` and `tools/list` cannot
drift apart. `VP_SCOPE` picks which shape `read_terminal` takes — the operator's
needs a project argument, the project advisor's already knows.

Every tool acts on the **agent**, never on the advisor. The advisor's own
context you clear yourself with 🗑 in the header (which drops both the stored
transcript and its `claude` session id, so the next turn starts cold), and its
model is fixed by the config's `[voice]` table. Keeping the advisor out of the tool surface
removes the ambiguity that would otherwise sit in every spoken "clear the
context" — there is only one thing it can mean.

Slash-command sends skip the reply watcher: a `/clear` has no reply worth
summarizing.

It never touches tmux or `state.json` directly — it calls back into this
server's own HTTP API (`/api/send`, `/api/pane`) on localhost, so a voice send
takes exactly the same path as the **Send ➤** button, keeps one writer for the
chat log, and shows up in the transcript. The port and token come from
the config's `[voice]` table; the project name, scope and tool list are passed in
(`VP_PROJECT`, `VP_SCOPE`, `VP_TOOLS`).

**tmux targets are matched exactly.** Every `-t` uses tmux's `=name` form
(`=name:` for pane targets). Without it tmux falls back to prefix matching, and
a project whose session is `bike` silently resolves to `bike-tracker` when no
`bike` exists — which would report the wrong session alive, capture the wrong
terminal, and type dispatched prompts into another project's agent.

## The dispatch skill

`plugin/` is a session-scoped Claude Code plugin holding one skill,
`voice-prompt:dispatch-to-agent` — how to word a prompt for an agent that can't
see the advisor conversation, when a request counts as "send it" versus
thinking out loud, what to say afterwards, and what to do when a send fails.
The advisor loads it before its first send.

It ships as a plugin (`--plugin-dir`) rather than `.claude/skills/` because the
advisor runs with the *project's* directory as its cwd, so a skill sitting here
would never be discovered — and a global one would show up in every unrelated
Claude Code session. Only the rules that must hold even if the skill is never
opened (send only when asked, never mid-brainstorm) live in the system prompt.

`send_tool = false` in the config's `[voice]` table withdraws the tools and the skill, leaving
tap-to-send as the only way in.

## Reply summaries

Every send starts a watcher thread for that project. A coding agent's TUI
redraws constantly while it works, so `summary_settle` seconds of an unchanged
pane is a reliable "it has stopped and is waiting". The watcher then runs one
advisor turn (`summary_model`, haiku by default) on the project's existing
advisor session and stores the reply as a `summary` message; the page picks it
up by polling `/api/updates`. A newer prompt supersedes the watcher in flight,
so you get one summary per prompt, not per keystroke. If the agent is still
going at `summary_timeout`, you get a summary that says so rather than silence.

Because it's an advisor turn rather than a separate summarizer, there's exactly
one summary — the same text the page shows and the phone speaks — and it lands
in the conversation you can ask follow-ups in. Set `summarize = false` to turn
the whole thing off and go back to auto-opening the terminal after a send.

## Multi-project mode

In Talk, the **☰ Operator** switch above the composer opens the **projects view**: every project on the machine, which
tmux sessions are up, which agents are working, and a tick against the ones
you're watching. The chat under it talks to an **operator** — the same mic,
Speak and Convo as anywhere else, but its context is every selected project's
terminal at once rather than one.

    ⚙ …
    2 of 27 watched                    [All] [None]
    ☑ ● api              agent working…      Open ›
    ☑ ● site             session up          Open ›
    ☐ ● radio-room       session up          Open ›

**Selection is the working set.** Ticking a project attaches its terminal to
every operator turn; the operator can do the same itself when you say "watch
garmin and ebook, drop the rest". Nothing is selected until you choose —
attaching 27 terminals to every turn is not a sane default — and at most
`operator_max_panes` ride along at once, with the rest reachable through
`read_terminal` on request.

**Round-ups** come from 📊, or by asking. The operator names each project
explicitly, because "it" means nothing when five agents are running:

> Garmin just finished and is waiting on you — it committed a checkpoint but
> flagged that it went to main instead of a branch. Voice prompt is about
> eleven minutes in, almost done. Bike-tracker is deep in a PCB routing
> review, thirty-eight minutes.

**Diving in** is `Open ›`, or asking the operator to take you into a project.
That is where control lives: the operator deliberately cannot send prompts to
anything, so "tell garmin to run the tests" gets you handed to garmin's own
advisor, which can. Going back is the ☰ Operator switch, or asking *that* advisor to
take you back — it has `open_projects_view` for exactly this. Every
conversation stays where it was, so the trip is free in both directions.

Summaries follow you. An agent finishing in a selected project surfaces in the
overview tagged with its name and is read aloud like any other, so the one
screen that watches everything isn't the one screen that never tells you
anything happened.

### Widescreen: three columns

At 1100px and up the page becomes three columns and stops hiding things:

    ┌─ operator ────┬─ project ─────────────┬─ its web app ────────┐
    │ round-ups,    │ ▣ garmin  ▣ ebook     │                      │
    │ selection,    │ ▣ mediacast           │   <iframe>           │
    │ hand-offs     │ ─────────────────     │                      │
    │               │ advisor chat          │   [main] [lan] ⟳ ↗   │
    └───────────────┴───────────────────────┴──────────────────────┘
       one composer, one mic, spanning the bottom

Both conversations are live at once. The view switch stops deciding what you
can see and decides only **which column the mic is wired to** — the focused
column's header lights up, and ☰ / `Open ›` / the voice tools move that focus
rather than swapping the screen. Summaries appear in whichever column owns the
conversation, but only the focused one is read aloud, so a project you're
watching from the overview doesn't get spoken twice.

The second column shows the **selected projects as tiles** and nothing else —
unselected projects aren't listed, and come back when you ask the operator for
them. `All…` in that column's header drops the full roster into the first
column if you'd rather point at it than say it.

The third column is the focused project's own web app, from `web_port` and
friends in the project list; `web_extra` entries become tabs. The host is
resolved in the browser, because only the browser knows which one it is — a
project configured as `localhost` is right at the desk and unreachable from the
phone, so a loopback host is rewritten to whatever host served this page.

**Knowing who you're talking to.** With two conversations live at once, the
one thing you must never have to guess is which of them the mic is wired to. A
switch sits directly above the composer — `[☰ Operator] [● garmin]` — and both
shows the answer and changes it. The placeholder agrees with it ("Ask the
operator…", "Ask about garmin…", "Send straight to garmin…"), and so does the
voice-state line while you're hands-free ("Listening for the operator…"), which
is the case where you can't look at anything. 📊 still reads Round-up against
the operator and Status against a project.

🗑 in the header clears whichever conversation has focus — its transcript *and*
its stored session id, so the next turn starts genuinely cold. On a desktop
each column header carries its own 🗑 as well, since with both transcripts on
screen "the focused one" is a promise rather than something you can see. That
clears the advisor conversation, not the agent's context: `/clear` to the
coding agent is a separate thing, and only a project advisor can do it.

**One limit worth knowing.** A page on HTTPS may not frame plain HTTP, and this
page is on HTTPS because the microphone needs a secure context. So a project
whose web app is plain http can't be embedded; the column says exactly that and
offers to open it in a tab. Reaching the voice server over its http port on localhost
from the machine itself — which is exempt from the redirect — embeds everything,
and so does putting the project behind TLS. Projects already fronted by
`tailscale serve` embed over https from anywhere.

**How it hangs together.** The operator is a conversation like any project's,
stored under the reserved key `@operator`, so history, reset and the chat turn
itself take the same path either way. It runs with no filesystem tools — it
spans projects, so there is no one directory to read — and reaches terminals
through `read_terminal` instead. Its tools are `select_projects`,
`open_project` and `read_terminal`; a project advisor gets its usual four plus
`open_projects_view`. `server.py` decides both the `--allowedTools` list and
the `VP_TOOLS` the MCP server publishes, so the two can't drift apart.

**The desktop window follows.** Each screen move (once its turn ends), each
session started and each web app brought up is appended to a numbered feed,
`GET /api/desk?after=<n>`. desktop/ reads it and puts the same thing in
front of you: the agent's terminal tab, the web app's tab, or the operator's
own. So "spin up garmin and take me there", said to a phone, leaves the desk
showing garmin's terminal.

A tool that moves the screen doesn't move it immediately: it records the intent,
`/api/chat` returns it with the reply, and the page acts once the reply has been
read out — so the words and the change arrive together instead of the screen
jumping mid-sentence.

## Speech

Both halves of the voice loop have two possible engines. Which one each browser
uses is set under **⚙** in the header and kept in that browser's
`localStorage` — the phone and the desktop want different answers.

### The browser's own

Safari and Chrome implement the Web Speech API. They stream the mic to their
own recognizer and hand back interim text as you speak, so the words appear in
the composer while you talk. That is the lowest-latency dictation available and
the right default on iOS.

**Firefox implements no speech recognition at all**, on any platform —
`SpeechRecognition` has never shipped, flag or no flag — so this path offers
Firefox no dictation whatever. Synthesis it does have: on Linux
`speechSynthesis` goes through speech-dispatcher (`apt install
speech-dispatcher speech-dispatcher-espeak-ng`), and with nothing installed the
voice list comes back empty and replies are silent. What it does have sounds
like espeak, because it is espeak — which is most of the reason Kokoro is
worth wiring up.

One more failure mode worth naming: Chrome recognizes by sending audio to a
Google service, and open-source Chromium builds are shipped without the API
keys for it. Those fail with a `network` error rather than a permission
prompt, and the page says so.

### Local servers

Point `stt_url` and `tts_url` at OpenAI-compatible endpoints and this server
proxies to them. Every browser then gets the same dictation and the same voice,
nothing is sent to a cloud, and Firefox joins in. Set either to `""` to fall
back to the browser for that half.

| Browser | Dictation, browser | Dictation, server | Replies, browser | Replies, Kokoro |
|---|---|---|---|---|
| iOS Safari | yes | yes | yes, good voices | yes |
| Chrome | yes (via Google) | yes | yes | yes |
| Chromium, no API keys | `network` error | yes | espeak, on Linux | yes |
| Firefox | **never** | yes | espeak, and only with speech-dispatcher | yes |

**Speech input** has three settings. *Auto* uses the browser's recognizer where
there is one and the server where there is not — so the same page does the low
latency thing on the phone and the working thing on Firefox. *Browser* and
*Local server* force one path, which is what you want if you would rather no
audio left the machine, or if Chrome's recognizer can't reach Google.

### Running them

Kokoro, for speech out. It is small (82M) and fast enough on CPU:

    docker run -d --name kokoro -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu:latest
    # NVIDIA:  --gpus all  and  ghcr.io/remsky/kokoro-fastapi-gpu:latest

Whisper, for speech in. Any OpenAI-compatible transcription endpoint works;
[speaches](https://github.com/speaches-ai/speaches) is the straightforward one.
Mount the HF cache so it reuses models you already have rather than pulling
them again:

    docker run -d --name speaches -p 8910:8000 \
      -v ~/.cache/huggingface:/root/.cache/huggingface \
      ghcr.io/speaches-ai/speaches:latest-cpu
    # NVIDIA:  --gpus all  and  :latest-cuda

`whisper.cpp`'s bundled server works too — point `stt_url` at its `/inference`
endpoint instead. The page always uploads 16 kHz mono WAV, which is the only
thing that server accepts and is fine for every other one.

Then check the voices arrived:

    curl -s localhost:$(proj port radio-room)/api/voices

One gotcha if you mount a HF cache that faster-whisper populated directly:
faster-whisper downloads only the four files it needs, and speaches asserts on
the model card (`stt.py`: `assert model_card_data is not None`), so the first
request dies with a 500 and an `AssertionError` naming the repo. Fetching the
rest of the repo metadata fixes it without re-downloading the weights:

    uvx --from huggingface_hub hf download Systran/faster-whisper-medium

For scale, on an RTX 3060 with `faster-whisper-medium`: transcribing four
seconds of speech takes about 0.4 s once the model is resident (the first
request pays a few seconds to load it), and a sentence of Kokoro comes back in
about 0.1 s. Both are comfortably inside the pause you'd leave anyway.

### How it works

`/api/stt`, `/api/tts` and `/api/voices` proxy to the two servers. Proxying
rather than calling them from the page directly is deliberate: the speech
servers bind to localhost, the phone is not on this machine, and going through
here means one origin, one token, and no CORS to arrange.

On the server path the page records the mic itself, through an
`AudioWorklet` — raw frames, rather than MediaRecorder's Opus-in-a-container
that would need decoding somewhere. Those same frames drive an adaptive silence
gate whose floor follows the room: hands-free ends a turn after 1.4 s of quiet,
push-to-talk after 2.6 s, because there the second tap is the real end signal
and a pause mid-thought shouldn't pre-empt it. What goes up is 16 kHz mono WAV,
resampled in the browser if it wouldn't give the graph that rate directly.

Replies are cut into sentence groups of ~230 characters either way. iOS
truncates long utterances, and on the Kokoro path short chunks are what let it
start talking before the whole reply is synthesized — each chunk is fetched
while the previous one plays. The speed buttons reach both engines: `rate` on
the browser utterance, `speed` in the Kokoro request.

## Music

Background music is an option of voice mode, **off by default**: ⚙ →
*Background music* turns it on for that browser, and only then does the music
bar appear in Talk (and the feed get fetched). `music = false` under
`[voice]` removes the option altogether.

`/api/music` fetches the musicforprogramming.net RSS feed server-side (the feed
sends no CORS header, so the browser can't read it directly) and returns the
episode list, cached for `music_ttl`. The audio itself the phone streams
straight from the source — it's HTTPS with `access-control-allow-origin: *` and
byte ranges, so no proxying is needed and pause/resume works properly. A random
episode is picked per load, and playback rolls into the next one when a mix
ends.

Autoplay is best-effort by nature: browsers refuse to start audio before the
page has been interacted with, always on iOS. If the initial `play()` is
refused the player arms itself and starts on your first tap anywhere. A pause
you make yourself is remembered across reloads; the automatic summary pause is
not, so with the option on, music comes back on the next load.

## Run

Turn on the `voice` feature, and register the voice project (`voice_project`,
default `radio-room`) with an HTTPS `web_port`, a `web_extra` named `http`, and
`web_start = "python3 voice/server.py --port $PORT"`. Then, the registered
way:

    proj up radio-room    # in tmux session web-radio-room (or its web_session)
    proj down radio-room

or by hand from here, which asks the registry for its ports:

    ./serve.sh        # foreground
    ./serve.sh -d     # detached in tmux session "vp-server"
    ./serve.sh -k     # stop

It listens on this machine only until `[voice] bind` says otherwise (see
[Security](#security)). To reach it from a phone, either set
`bind = "tailscale"`, or keep `"local"` and run `./tailscale-https.sh`, which
puts `tailscale serve` with a real certificate in front of it. Two listeners:

- `https://<host>:<web_port>` — the one to use. Mic access needs a secure context,
  so **voice input only works here**, on either speech path. A self-signed cert
  is generated on first run; accept the browser's warning once (in Safari,
  "Show Details" → "visit this website"). Note: on iOS, speech recognition can
  be unavailable in home-screen (standalone) web apps — if the mic fails there,
  use it in Safari itself.
- `http://<host>:<http port>` — redirects here, so an old bookmark still lands on a
  page with a working mic. It serves the page only when there is nowhere to
  redirect to (the cert could not be generated), from
  localhost (already a secure context), or behind a TLS-terminating proxy that
  sets `X-Forwarded-Proto: https` — which is what `tailscale-https.sh` sets up.
  `/api` is never redirected: the MCP server, and any agent hooks you wire
  up, post to it over plain http and don't follow redirects. If you added a home-screen
  icon from the http URL, re-add it from the https one — a standalone web app
  follows the redirect out into Safari rather than staying in the app.

Requirements: Python 3.11+, tmux, the `claude` CLI, docker (for containerized
projects). No pip dependencies.

## Configuration

See the `[voice]` table in `config.example.toml` — pane capture size, model,
the local speech servers (`stt_url` / `tts_url`, see [Speech](#speech)), where
it listens and its access token, and per-project tmux target overrides.

The ports are the registry's, never the config's: the HTTPS listener is the
voice project's `web_port` (`--port`, then `$PORT`, then the registry), and
the plain-HTTP redirector its `web_extra` named `http` (`--http-port`,
`$HTTP_PORT`). With no certificate the main port serves plain HTTP.

## Security

This server can submit prompts to your agent sessions, and an agent can run
commands. Treat reaching it as reaching a shell.

- `bind = "local"` (the default) listens on 127.0.0.1 only.
- `bind = "tailscale"` adds the tailnet address, not the LAN. Like the
  [browser terminal](../terminal/README.md#security), it treats the tailnet as
  the boundary.
- Anything wider (`"any"`, a LAN address) refuses to start without
  `token = "..."` under `[voice]`. The UI asks for the token once. Set
  `allow_no_token = true` only for a network you trust some other way.

The self-signed TLS certificate stops eavesdropping but proves nothing about
who you connected to; `tailscale-https.sh` gives a real one.

## Development

    ../launch.sh       # attach (or create) the repo's "radio-room" claude session
    ../launch.sh -r    # force-restart the session

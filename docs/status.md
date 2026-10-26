# Agent status

Every agent session gets a state: working, waiting on you, done, and so on.
That's what the desktop window's cards and the voice server's home screen
show. It comes from two sources writing into one store:

```
 agent's own hooks ──► voice/hooks/vp_event.py ──┐
                                                 ├──► events.db ──► status.py ──► desktop window
 tmux panes ──► the pane monitor (status.py) ────┘                           └──► voice server
```

| File | Role |
|---|---|
| `voice/events.py` | the store (SQLite) and the state machine; transcript readers; pane classifier |
| `voice/hooks/vp_event.py` | run by the agents' hooks: one event per call |
| `voice/hooks/install.py` | wires the hook into each agent's config |
| `voice/status.py` | session map, pane monitor, summaries, usage, the instances payload |
| `voice/summarize.py` | the local-model summary (feature `summaries`) |
| `voice/usage.py` | quotas and token rollups (feature `usage`) |

## States

| state | meaning |
|---|---|
| `starting` | a session just started; nothing asked of it yet |
| `working` | a prompt went in and the agent is on it |
| `needs_input` | it ended its turn on a question, or is holding a menu, permission dialog or trust prompt |
| `completed` | it finished something you haven't looked at |
| `idle` | nothing pending: dismissed, cleared or cancelled |
| `error` | the turn ended in an error |
| `offline` | the agent exited (its pane is back at a shell) |

`completed`, `needs_input` and `error` stay until dismissed or until the agent
is prompted again. Cards are ordered most recently changed first: a card's
time is its last change of state (marking it seen doesn't count), else when
its session started, else when its project was added.

A project added in the last `new_project_window` seconds (3 days) that has
never run an agent gets a card of its own, state `new`, until its agent starts
or it is marked seen. `proj add` stamps `added` on the registry entry for this.

A pinned project (📌 on its card, or `pin_agent = true` on its registry entry)
keeps its card whatever it is doing, `offline` when it has no session, and
pinned cards come first. On the page they get a Pinned section at the top.
Pinning in the Projects view (`pin_project`) is separate and doesn't move a
card here.

A turn that ends on a question counts as `needs_input`, not `completed`. That
means a question in the last sentence or two, a question put to "you"
anywhere in the last paragraph, or an ask without a question mark ("Let me
know which…", "Want me to…").

Every change is a row in `events`, with the agent's final message, its
transcript path and the last 60 lines of its pane. Rows are kept 30 days.

## The store

`voice/events.db` (SQLite, WAL), or `$VP_EVENTS_DB`. It's ignored by git.
Its tables are `events`, `instances` (each instance's current state),
`sessions` (tmux session → project and agent, which the hook resolves
against), `summaries`, and usage tables.

The hook writes to it directly: no server, no port, a process start and an
insert.

## Hooks

`vp_event.py` runs inside the agent's own hook and must never get in its
way: it always exits 0, and errors go to `voice/hooks/errors.log`. It knows
which project fired it from `$TMUX_PANE` (the pane's session, looked up in
`sessions`). It does nothing outside tmux, or with `VP_STATUS_HOOK=off`,
which the voice advisor's own `claude -p` runs set.

| agent | config file | events |
|---|---|---|
| Claude Code | `~/.claude/settings.json` | `SessionStart`, `UserPromptSubmit`, `Stop` (final message, background tasks), `StopFailure`, `Notification` (permission prompt → `needs_input`), `SessionEnd` |
| Antigravity | `~/.gemini/config/hooks.json` (one named hook) | `SessionStart`, `PreInvocation` (only a turn's first call counts), `Stop` (`terminationReason` tells done, cancelled and error apart) |
| Copilot CLI | `~/.copilot/hooks/radio-room.json` (`$COPILOT_HOME`) | `sessionStart`, `userPromptSubmitted`, `agentStop`, `errorOccurred` (unrecoverable only), `notification`, `sessionEnd` |

Antigravity's and Copilot's payloads don't name their event, so the command
line does (`vp_event.py copilot agentStop`). Subagents' Copilot hooks are
ignored; only the main agent's turn moves the card.

The final message comes from Claude's payload or transcript, agy's
transcript, or Copilot's `agentStop` `transcriptPath`. Copilot's transcript
format isn't documented, so that reader takes the newest record that looks
like an assistant message. If it finds nothing, the pane tail still rides
with the event.

```sh
python3 voice/hooks/install.py               # show the change, write nothing
python3 voice/hooks/install.py --apply       # write it (.bak copies kept)
python3 voice/hooks/install.py --remove --apply
python3 voice/hooks/install.py --agents copilot --apply
```

By default only the agents offered on this machine are touched (see
`core/README.md`, Agents). In shared files, only entries that run
`vp_event.py` are added or removed. Agents pick the hooks up when they next
start.

**Containers.** An agent in a container that mounts `~/.claude` (or
`~/.gemini`, `~/.copilot`) runs the same hook, so it also needs `voice/`
mounted at its host path, read-write, and `VP_CONTAINER=<container>` in its
environment. That turns its own tmux's `default` into the
`<container>/default` the session map knows.

## The pane monitor

Every `status_poll` seconds (default 5), the monitor reads the last lines of
each live agent pane and classifies it as one of:

- **asking**: a menu, permission dialog or trust prompt → `needs_input`
  (agy fires no hook while it holds one);
- **shell**: the agent exited and `exec bash` left a prompt → `offline`;
- **busy** / **idle**: for sessions no hook has reported on since they
  started (containers, sessions older than the hooks, agents without hooks),
  these edges *are* the turns. For hooked sessions they only correct a
  state that is plainly wrong, and only after it has held for two polls.

A `working` state whose pane has sat idle for `status_settle` seconds
(default 30) is ended there too, because its Stop never arrived.

What each agent's pane looks like is a set of regexes in `events.py`
(`_ASKING`, `_BUSY`, `_IDLE`), keyed by agent type. **Copilot's are a best
guess, not yet checked against a live session.** Override them, or supply
them for an agent that isn't built in, in the config:

```toml
[agent.copilot]
pane_busy = "(?i)esc to cancel"
pane_idle = "@ to mention|/ for commands"
pane_asking = "Do you want to |No, and tell Copilot"
```

They're matched against the pane's last 14 non-blank lines.

**One monitor per machine.** The desktop window and the voice server both
call `status.start()`. A background thread takes an exclusive lock on
`monitor.lock` beside `events.db`, and only the holder runs the monitor,
summaries and usage. If the holder exits, another takes over. Any number of
processes read.

**Shared sessions.** When several registry entries point at the same
session, the first one owns the agent and the rest are listed as its
aliases.

## Summaries (feature `summaries`)

Each agent's newest finished turn gets a one-line rough summary from a local
model through Ollama (`[status] summary_local_model`, default `granite4:1b`).
It's always shown labelled as a machine summary, next to the full message.
The model answers two yes/no questions (does it ask the user, did it fail)
and writes the line. Its answers are a hint only and never move a card; the
state stays rule-based. A failure is recorded and retried after
`summary_retry` seconds, backing off.

Settings: `summary_ollama_url`, `summary_num_gpu` (`0` = CPU only, `-1` =
Ollama decides), `summary_num_ctx`, `summary_keep_alive`,
`summary_local_timeout`, `summary_window` (only turns this recent).

## Usage (feature `usage`)

Claude Code and Antigravity only:

- **Service quotas**: Claude Code's 5h / 7d windows and credit grants from
  `~/.claude.json`; Antigravity's buckets and credits by polling
  `agy -p /usage` and `/credits` (every 5 minutes, or 30 s after a turn).
- **Token rollups** per project and instance (5h / 24h / 7d), indexed from
  `~/.claude/projects/**/*.jsonl` and agy's transcripts and attributed to
  projects by hook mappings and working directories. Transcript directories
  that aren't a project's own go in `[status] dir_aliases`.

With the feature off, none of this runs and no quota meters are shown.

## Reading it

`status.instances_payload()` is what both front ends show: every running
instance, plus dead ones still holding something unseen and new projects,
most recently changed first,
each with its state, since when, the final message excerpt and summary,
background tasks, aliases and usage.

The voice server serves it as `GET /api/instances`, with `GET /api/event?id=`
(one event in full) and `POST /api/instances/dismiss {project, agent}`.

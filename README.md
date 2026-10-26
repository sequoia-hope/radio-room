# Radio Room

One place to see and steer the coding agents on a machine — Claude Code,
Antigravity and Copilot CLI, each in its own tmux session — from a desktop
window with real terminals, and optionally from a phone over
[Tailscale](#from-a-phone-tailscale).

The core is small: a project list, an agent session per project, and a
status for every agent (working, needs input, done). Everything else is
optional and off until the config turns it on.

    core/        the project registry: which projects exist, where, which tmux
                 session runs each agent, and which port serves each web app.
                 `proj` is its CLI, hub.py a web dashboard of it.
    desktop/     a GTK window: the agents down the side, real (VTE) terminals
                 attached to their sessions, and the projects to start them
    voice/       agent status (status.py, events.py, hooks/) — and, as the
                 "voice" feature, a phone UI with a Sonnet advisor and speech
    terminal/    a browser terminal into any agent's session, for the phone
    reserve/     a CPU reservation queue: heavy jobs book cores and wait

## How it fits together

```
 ~/.config/radio-room/config.toml ─► core/config.py
 projects.toml ──────────────────► core/registry.py ◄── core/git.py, core/jira.py
                                     │   ▲
                         proj (CLI) ─┘   │ status(): projects, agents, git, tickets
                                         │
 agents' hooks ─► voice/hooks ─► events.db ◄─ pane monitor ─ voice/status.py
                                                                 │ instances
                                     ┌───────────────────────────┤
                                     ▼                           ▼
                           desktop/ (GTK window)        voice/server.py (phone)
                           VTE tabs = tmux attach       terminal/ (browser tabs)
```

- **The registry** is the one list of projects. Every other part reads it;
  only `proj` and the window's worktree, link and remove actions write it.
- **Each agent runs in tmux**, one session per project and agent, so it
  survives every front end. The window, the browser terminal and a plain
  `tmux attach` are all just clients.
- **Status** is written by the agents' own hooks and by a pane monitor into
  one SQLite store. Whichever of the window and the voice server starts first
  runs the monitor; both read the store.
- **Nothing is a daemon you have to keep alive** for the core to work: the
  window alone gives projects, terminals, status, worktrees and tickets.

## Documentation

| | |
|---|---|
| [core/README.md](core/README.md) | the registry and `proj`: the project file, agents and sessions, worktrees, Jira, ports, `proj doctor`, the hub |
| [docs/status.md](docs/status.md) | agent states: hooks for each agent, the pane monitor, summaries, usage |
| [desktop/README.md](desktop/README.md) | the window: agents, projects, tickets, tabs, links, the monitor |
| [voice/README.md](voice/README.md) | the voice server and Sonnet advisor (feature `voice`) |
| [terminal/README.md](terminal/README.md) | the browser terminal, for reaching sessions from a phone |
| [reserve/README.md](reserve/README.md) | the CPU reservation queue |
| [config.example.toml](config.example.toml) | every setting, with its default |

## Setup

Linux, Python 3.11+, tmux, and for the window GTK 3 with VTE and WebKit2
(`gir1.2-vte-2.91 gir1.2-webkit2-4.1 python3-gi` on Debian/Ubuntu). Nothing
from PyPI.

    ./install.sh

links `proj` and `radio-room` into `~/.local/bin`, puts the window in
the app grid, and writes `~/.config/radio-room/config.toml` from
`config.example.toml` if there isn't one. Nothing personal is kept in the
repository: the config, the project list and the agents' event store all
live outside it (or are ignored).

Then register projects and open the window:

    proj add api ~/src/api --no-web       # a project with no web app
    proj add site ~/src/site              # one with a port from the pool
    proj agent api                        # start its agent and attach
    radio-room

`proj agent NAME [AGENT]` starts the session the registry's way (`-r`
restarts it, `-d` leaves it detached), `proj stop` kills it, and the window's
Projects view does the same with a click.

### Agents

`claude`, `agy` and `copilot` are built in. Those whose command is on PATH
are offered, in that order, and the first is the default (it runs in the
project's base session; others get `<session>-<agent>`). To choose, or to
change a command:

```toml
agents = ["copilot"]

[agent.copilot]
cmd = "copilot --allow-all-tools"
```

An agent that isn't built in is just another `[agent.<id>]` with a `cmd`.

### Worktrees

A project is a directory, so several projects can be one repository checked
out in different worktrees — or subdirectories of one, in a monorepo — and
standalone repos and plain directories work the same. The window groups
projects by repository and shows each one's branch.

    proj add login --worktree mono --jira ABC-123 --subdir services/api
    proj worktrees                  # every checkout, registered or not
    proj rm login --worktree        # and the worktree, unless it has changes

`--worktree` takes a registered project or a path; the branch is the ticket's
(`ABC-123-<summary>`), `--branch`, or the project's name, made from `--from`
when new. Where worktrees go is `worktree_dir` in the config. In the window,
each repository's heading has a "+" for the same. More in
[core/README.md](core/README.md#worktrees).

### Jira

With `[jira]` configured (Cloud or Data Center; see `config.example.toml`),
projects link to tickets — explicitly, or by naming one in their branch — and
the window gets a Tickets view: your tickets (the config's JQL) with the
projects linked to each, and on each ticket: open it, start work on it in a
new worktree, send it to a linked agent as a prompt, link or unlink it, move
it, comment, assign it to yourself. "+" opens a new one. From the shell:

    proj jira                       # your tickets
    proj jira show ABC-123
    proj jira link login ABC-123
    proj jira move ABC-123 "In Progress"
    proj jira comment ABC-123 "Fixed on the branch"
    proj jira new "Flaky test in api" --link login

The token comes from a command, an environment variable or a file, never the
config. More in [core/README.md](core/README.md#jira).

### Status

Each agent's state comes from two places. Its own hooks report prompts,
finished turns, errors and permission prompts:

    python3 voice/hooks/install.py           # show the change
    python3 voice/hooks/install.py --apply   # make it

which edits only the agents offered here: `~/.claude/settings.json`,
`~/.gemini/config/hooks.json`, or `~/.copilot/hooks/radio-room.json`. And a
pane monitor reads each agent's terminal for what hooks can't say — a menu
held open, an agent that exited, a session with no hooks at all.

The monitor runs in whichever of the desktop window and the voice server
starts first. For Copilot CLI its patterns are a best guess not yet checked
against a live session; if a Copilot card sits in the wrong state, set
`pane_busy`, `pane_idle` or `pane_asking` (regexes over the pane's last
lines) under `[agent.copilot]`. The whole story is
[docs/status.md](docs/status.md).

### Optional parts

```toml
[features]
voice = true       # the phone UI and advisor; the window shows its page
launcher = true    # a launcher tab in the window, for a ~/.bashrc launcher
usage = true       # Claude Code / Antigravity quotas and per-project tokens
summaries = true   # one-line turn summaries from a local Ollama model
```

Some things switch themselves on when what they need is present: the GPU
meter with `nvidia-smi`, the CPU booking meter with a registered `reserve`,
container sessions with Docker, a project's web tab with a `web_port`.

The voice server is a registry project of its own (`voice_project`, default
`radio-room`): register it with an HTTPS `web_port`, and a `web_extra` named
`http` for the plain-HTTP redirector, then `proj up radio-room`.

## From a phone: Tailscale

The desktop window needs no network at all. **Everything you reach from a
phone assumes [Tailscale](https://tailscale.com)** — the tailnet is the
security boundary, and nothing here is meant to face the open internet or a
shared LAN.

| part | listens on, by default | from a phone |
|---|---|---|
| voice server | this machine only (`[voice] bind = "local"`) | `bind = "tailscale"`, or `voice/tailscale-https.sh` for a real certificate |
| browser terminal | the `tailscale0` address only; **refuses to start without it** | as is: it serves an unauthenticated shell, so the tailnet is its only lock |
| hub | this machine only (`[hub] bind`) | `bind = "tailscale"` |
| CPU queue | this machine only (`[reserve] bind`) | `bind = "tailscale"`; `"any"` for docker containers |

`bind` takes `"local"`, `"tailscale"` (this machine plus the tailnet, not
the LAN), `"any"`, or an address. The voice server can prompt your agents,
so it refuses anything wider than `"tailscale"` without a `token`. The hub
and the CPU queue have no auth at all; they say so when they start on a wider
address. Your tailnet's ACLs decide which of your devices can reach them —
anyone you share the machine with on Tailscale can too.

## Ports

No port is ever written into the code or the config. Every server asks the
registry: an explicit `--port`, then `$PORT` (set when `proj up` or the hub
starts it), then `proj port`, which resolves the current directory to its
project — and fails loudly if none of those answer. `proj doctor` audits what
is listening against the registry.

## Troubleshooting

**A card is stuck in the wrong state.** Check the hooks are installed for
that agent (`python3 voice/hooks/install.py` should say "already as
wanted") and that the agent was started after installing them. Hook errors
go to `voice/hooks/errors.log`. Agents without hooks rely on the pane
monitor alone; for Copilot, tune its `pane_*` patterns (see
[docs/status.md](docs/status.md#the-pane-monitor)). A `working` card whose
turn plainly ended settles after `status_settle` seconds (default 30).

**No cards at all.** The agent's session has to be the one the registry
expects (`proj list` shows which sessions are live), and something must be
running the monitor: the window or the voice server. Hooks only map a
session once the monitor has synced it.

**An agent's button is missing.** Only the agents in `registry.AGENTS` are
offered: those listed in `agents`, or those on PATH when the list is empty.
`python3 -c "import sys; sys.path.insert(0,'core'); import registry;
print(registry.AGENTS)"` shows what the config produced.

**Jira says no token, or 401.** Run the `token_cmd` by hand. Cloud needs
`email` plus an API token; Data Center needs `email` empty and a personal
access token. `proj jira` shows the error Jira returned.

**A repository's worktrees don't group together, or one is missing.** The
window groups by git's common directory, and only shows registered
projects. `proj worktrees` lists every checkout and marks the unregistered
ones; `proj add NAME DIR` registers an existing one.

**`proj` says no port.** The project isn't registered with a `web_port`, or
the command was run outside its directory. `proj port NAME` names it
outright. Nothing falls back to a default, on purpose.

**After changing the config,** restart what reads it: the window, the voice
server (`proj down radio-room; proj up radio-room`), the hub (`core/hub.sh -r`).

## Development

    python3 -m pytest                    # every suite; nothing real is touched
    proj doctor                          # after changing how something serves
    ./launch.sh                          # this repo's own agent session ("radio-room")

CI (`.github/workflows/ci.yml`) runs the same on Python 3.11–3.13, and the
desktop window's tests against the distro's GTK under Xvfb.

## License

MIT — see [LICENSE](LICENSE). The vendored xterm.js under
`terminal/static/vendor/` is MIT too ([its notice](terminal/static/vendor/LICENSE-xterm.txt)).

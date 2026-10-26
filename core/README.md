# core

The project registry and everything that reads it: which projects exist, where
they live, which tmux session runs each agent, which port serves each web app,
which git checkout each is, and which Jira tickets each is for.

| File | Role |
|---|---|
| `config.py` | Radio Room's settings, from `~/.config/radio-room/config.toml` |
| `registry.py` | the project list: load/save, agents and their sessions, ports, status |
| `proj` | the CLI over all of it |
| `git.py` | a project's repo, branch and worktree; making and removing worktrees |
| `jira.py` | the Jira client, and ticket keys in branch names |
| `hub.py`, `hub.sh` | a web dashboard of the registry (optional) |
| `test_core.py` | tests: Jira against a fake server, worktrees in a scratch repo |

Everything is standard library only (Python 3.11+), so the agents' hooks and
the desktop window can import it under whatever `python3` they find.

## Configuration

`config.py` reads `$RADIO_ROOM_CONFIG`, else `~/.config/radio-room/config.toml`.
A missing file means all defaults. `config.example.toml` at the repo root
documents every key. In short:

```toml
projects = "~/.config/radio-room/projects.toml"   # the project list
agents = []                                      # [] = those on PATH
worktree_dir = "{parent}/{reponame}.worktrees/{name}"
voice_project = "radio-room"

[agent.copilot]        # per-agent overrides, or new agents
[features]             # voice, launcher, usage, summaries — all off by default
[jira]                 # on when url is set
[status]               # the agent status monitor (see ../docs/status.md)
[voice]                # the voice server (see ../voice/README.md)
```

`$RADIO_ROOM_PROJECTS` overrides `projects` for one command, which is handy for
trying things against a scratch list.

## The project list

A TOML file, written by `proj` and safe to edit by hand. Only `name` and `dir`
are required:

```toml
[hub]
port     = 8000             # the dashboard
bind     = "local"          # or "tailscale", "any", an address
pool     = [8010, 8099]     # what the allocator hands out
reserved = []               # ports in the pool never to hand out

[[projects]]
name = "api"
dir  = "~/src/mono.worktrees/api/services/api"
jira = ["ABC-123"]                    # tickets linked by hand
tmux = "api"                          # the agent's session (default: the name)
command = "./launch.sh"               # or: a launch script whose SESSION= names it
agent_cmd = { claude = "claude --model opus" }   # this project's own agent commands
container_dir = "/workspace"          # for a project whose agents run in Docker

web_port  = 8012                      # see "Ports"
web_fixed = false
web_start = "./serve.sh"
web_stop  = "systemctl --user stop x" # only when something else supervises it
web_desc  = "One line for the dashboard"
web_host  = "10.0.0.5"                # only when it binds one specific address
web_scheme = "https"
web_path  = "/"
web_url   = "https://api.example"     # a proxy in front of web_port
web_extra = [{ name = "http", port = 8013, path = "/" }]
```

The schema is also at the top of `registry.py`. The registry writes the file
itself (`registry.dumps`), so writing needs no third-party package.

## Agents

`registry.AGENTS` is the agents this machine offers, built from
`BUILTIN_AGENTS` (`claude`, `agy`, `copilot`), the config's `[agent.<id>]`
overrides, and its `agents` list. With an empty list, it is every built-in
agent whose command is on PATH. The first one is the **default**:

- it runs in the project's **base session**: `tmux`, else the `SESSION=` (or
  `CONTAINER=`/`DEFAULT_SESSION=`) line of the project's `command` script,
  else the project's name with `.` and `:` made safe;
- every other agent runs beside it in `<base><suffix>`: `api-agy`,
  `api-copilot`. Inside a container it is `<suffix without the dash>` beside
  `default`.

`agent_target(p, agent)` answers "where is it", `start_agent` creates the
session detached, `launch_agent` creates it and then execs `tmux attach` (or
`switch-client` inside tmux, or `docker exec … tmux attach` for a container),
and `stop_agent` kills it. Every tmux target is anchored with `=`, because a
bare target is a prefix match: without it, `finance` finds `finance-llm`.

From the shell:

```sh
proj agent api            # start the default agent if needed, attach
proj agent api agy -r     # restart agy's session first
proj agent api -d         # start it, don't attach
proj stop api copilot
```

## Worktrees

A project is a directory, so a project can be:

- a standalone repository, or a directory inside one;
- one of several worktrees of the same repository, each on its own branch.
  That's the usual monorepo case, often a subdirectory such as
  `services/api`;
- not in git at all.

`git.info(dir)` tells these apart and is what `status()` reports as `git`:

| key | |
|---|---|
| `repo` | the repository's common git dir, the same for every worktree. This is what groups projects |
| `name` | the main checkout's directory name |
| `root` | this checkout's top level |
| `subdir` | the project's place inside it (`""` at the top) |
| `branch` | `None` when detached |
| `worktree` | `True` for a linked worktree, `False` for the main checkout |

Answers are cached for 15 s per directory. A status pass over 60 projects
costs about 0.1 s.

```sh
proj add login --worktree mono --jira ABC-123 --subdir services/api
proj add nav --worktree ~/src/mono --branch nav-bar --from origin/main
proj worktrees                  # every checkout of every repo in use
proj rm login --worktree        # unregister and remove the checkout
```

`--worktree` takes a registered project's name or any path inside the
repository. The branch is `--branch`; else, with `--jira`, the ticket's key
and summary (`ABC-123-login-fails-on-safari`, if Jira is reachable, otherwise
just the key); else the project's name. An existing branch is checked out.
A new one is made from `--from`, defaulting to HEAD. The checkout goes
where `worktree_dir` says, and the project is registered without a web
instance.

`proj rm --worktree` keeps the branch. It refuses when the worktree has
uncommitted changes (`--force` overrides), when another registered project
lives in the same checkout, and always for a main checkout.

## Jira

`jira.py` talks REST to Jira Cloud or Data Center. It asks `serverInfo`
which one it is talking to rather than being told:

| | Cloud | Data Center |
|---|---|---|
| auth | basic, `email` + API token | bearer, personal access token (leave `email` empty) |
| search | `/rest/api/3/search/jql` | `/rest/api/2/search` |
| assign | by `accountId` | by user `name` |

Everything else (issues, transitions, comments, create) is REST v2 on both,
so descriptions and comments are plain text or wiki markup.

The token is never in the config file. It comes from `token_cmd` (a command
that prints it, e.g. `secret-tool lookup service jira`), else the environment
variable `token_env` (default `JIRA_TOKEN`), else `token_file`.

**Links.** A project's tickets (`registry.tickets(p)`, and `tickets` in
`status()`) are its `jira` list, then any keys its branch names. With
`[jira] projects = ["ABC"]`, any case counts (`abc-123-fix`). Without it, only
upper-case `KEY-123` does, so a branch called `utf-8-cleanup` isn't read as a
ticket. Links from a branch come and go with the branch; only the explicit
ones can be unlinked.

```sh
proj jira                          # your tickets (the config's jql)
proj jira list --jql "project = ABC AND sprint in openSprints()"
proj jira show ABC-123             # description and the last few comments
proj jira link login ABC-123       # or unlink
proj jira move ABC-123 "In Progress"   # a status, a transition name, or an id
proj jira comment ABC-123 "Fixed on the branch"   # "-" reads stdin
proj jira new "Flaky test in api" --desc "..." --link login
```

## Ports

The registry owns every port number, and a project asks for its own. No copy
is ever kept inside the project.

- **Pushed:** when `proj up` or the hub starts a project, `$PORT` is
  substituted into `web_start` and exported.
- **Pulled:** `proj port` prints the port of the project the current
  directory belongs to (the deepest registered directory containing it).

So every server's order is an explicit flag, then `$PORT`, then `proj port`,
then **fail**. There is no fallback default: a silent wrong port is how two
projects end up fighting over one number. `proj add` scaffolds exactly this
as `serve.sh` for a project that takes a port.

There are two classes of port:

- **allocated** (`web_fixed = false`): drawn from `[hub].pool`, handed over as
  `$PORT`. Use this for anything new.
- **adopted** (`web_fixed = true`): a port the project hardcodes for its own
  reasons, such as a tool's well-known default or a compose file. It is
  recorded so the dashboard links correctly and the allocator never reuses it.

The allocator skips a port that is registered, reserved, or currently
listening.

A web instance runs in a tmux session, `web-<project>` (or `web_session`).
`proj down` kills that and then anything still holding the port, since some
start scripts daemonise away from their pane. A project with `web_stop` is
supervised elsewhere (systemd, its own scripts), so its own start and stop
commands are run instead. One with no `web_start` is link-only.

```sh
proj add site ~/src/site          # allocate a port, scaffold a status page + serve.sh
proj add tool ~/src/tool --fixed-port 8189
proj up site | proj down site | proj url site | proj port site
proj set site web_scheme=https
proj list | proj ports | proj alloc
```

## Auditing

`proj doctor` checks the registry against what the machine is doing:

- a port claimed by two projects;
- an allocated project whose `web_start` never receives `$PORT`;
- something listening inside a project's tree on a port nobody registered (an
  ad-hoc `http.server`). Tooling that merely inherited a working directory is
  only a warning;
- a directory being served that no project owns;
- a project's own port written into its source, or a bind-shaped default
  naming another project's port.

Errors exit non-zero. Run it after changing how a project serves itself.

## The hub

`./hub.sh -d` runs the dashboard (`[hub].port`) in a tmux session named
`hub`: every project, live status, start/stop for managed web instances. It
binds `[hub].bind` — `"local"` by default, or `"tailscale"`, `"any"` or an
address — and its start/stop endpoints are unauthenticated, so think before
widening it past `"tailscale"`. The desktop window's Projects view does the same job, so the
hub is optional.

## Tests

    python3 core/test_core.py

These run against a scratch config, project list and git repository in a
temp directory, and a fake Jira on an ephemeral port. Nothing real is
touched.

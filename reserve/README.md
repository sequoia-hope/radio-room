# reserve

A reservation queue for this machine's CPU. Heavy jobs book cores before
they start and wait their turn, instead of every project's builds and test
suites piling onto the same cores at once.

    ~/.claude/bin/reserve -c 8 -l "workspace tests" -- cargo test --workspace
    ~/.claude/bin/reserve --status
    proj up reserve           # the server; its page is `proj url reserve`

## How it decides

- **Bookable cores** are the machine's minus `--keep-free` (default 4), which
  are never booked: the desktop, the voice operator, STT/TTS and the agents
  themselves always have room.
- **Strict first come, first served.** The head of the line starts when its
  cores are free; nobody behind it jumps ahead, even when a smaller job would
  fit.
- **Liveness, not trust.** Waiters and holders check in every 3 s; one quiet
  for 20 s (its client was killed) is dropped.
- **Saved as it changes** (`~/.local/state/reserve/book.json`, not on
  check-ins): a restart keeps every booking's id and place in line, so
  clients carry on as if nothing happened. A client whose booking was lost
  anyway re-books, a waiter at its original place.

## Steering it

From this server's page, Radio Room's page (a CPU panel on the Agents view;
each agent's card also says when its project is waiting or holding cores),
the desktop window's CPU tab, or by voice (the operator's `cpu_queue` and
`cpu_control` tools):

- **stop** a running job: on the host its scope is stopped (SIGTERM, then
  SIGKILL); a client also kills its command when its check-in says `stop`,
  which is how a container job is stopped.
- **front**, **cores** (shrink a waiter so it fits sooner) or **cancel** a
  waiting job.
- **pause** granting: running jobs carry on, nothing new starts.
- **kept free**: how many cores are never booked, changed live and saved.

The last 40 jobs to finish are kept, with how they ended (done, stopped,
cancelled, client vanished) and their exit code.

POST `/api/{stop,front,cancel}/<id>`, `/api/cores/<id>` `{cores}`,
`/api/settings` `{paused?, keep_free?}`; GET `/api/state`.

## What a granted job gets

- `CARGO_BUILD_JOBS`, `MAKEFLAGS=-jN`, `RUST_TEST_THREADS`,
  `NEXTEST_TEST_THREADS`, `RAYON_NUM_THREADS`, `OMP_NUM_THREADS`,
  `CMAKE_BUILD_PARALLEL_LEVEL` and `RESERVED_CORES`, all set to N.
- **On the host**, a systemd user scope (`reserve-<id>.scope`) with
  `CPUQuota=N×100%`, so the job cannot use more than it booked however many
  threads it starts.
- **In a container**, only the environment: its cgroup is not delegated, so
  there is nothing to put a quota on.

## Where the client lives

`reserve` beside this file is the source. At every start the server copies
it to `~/.claude/bin/reserve` and writes its port to `~/.claude/reserve.json`.
`~/.claude` is the directory a host and its Claude Code containers usually
share (mounted into the container), so the same path works in every agent with
nothing to install. On the host the client finds the server with
`proj url reserve`; in a container, from that file at the docker gateway.
`/client` on the server serves it too.

The server listens on `[reserve] bind` in the config (or `--bind`): `"local"`
by default. A container reaches the host through the docker gateway, not
loopback, so serving containers takes `"any"`. The page and its POSTs
(cancel, stop, pause) are unauthenticated, so only widen it on a network you
trust.

Agents learn to use it from their instructions — for Claude Code, a section
in `~/.claude/CLAUDE.md` along these lines:

    Wrap any heavy command (big builds, full test suites, benchmarks) in
    ~/.claude/bin/reserve, which books cores and waits its turn:
        ~/.claude/bin/reserve -c 8 -l "workspace tests" -- cargo test --workspace

## Who asked

A booking is credited to the registered project its cwd is in. Outside every
project, a linked worktree counts as the same place in its repository's main
checkout (`~/x-worktrees/feature/games/y` is `games/y` of `~/x`), and failing
that the client's tmux session names the agent that asked.

## The page

The book (running, waiting, free cores), and beside it what each project is
actually using right now, booked or not. That second table is what catches a
job that never booked. A process is credited to a container by its cgroup
(and the container to a project by its compose directory, or the registry
entry that starts it), otherwise to the project its cwd is in, walking up to
the agent when a tool runs from a scratch directory.

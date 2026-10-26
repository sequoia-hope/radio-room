# desktop

Radio Room as a desktop window: every agent's card down the side, real
terminals beside them.

    desktop/radio_room.py        # or `radio-room`, once installed

The browser terminal (terminal/) exists so a phone can reach a session.
At the desk that's a detour — xterm.js over a WebSocket to a PTY the server
holds — when a native terminal can just be the tmux client. Here each
terminal is VTE (GNOME Terminal's widget) running the attach itself.

- **The Agents view** is every running agent (and any dead one still
  holding something unseen, and any project added lately that has never run
  an agent — clicking that card starts it), read every 3 s straight from
  voice/status.py — the voice server need not run — most recently changed
  first: state,
  then the final message (or its rough summary, with the `summaries`
  feature) and, with `usage`, its 5h / 7d tokens. Under each card are its
  project's other controls — its other agents and `web`, as in the Projects
  view — so a second agent or the web app is one click away. A finished,
  waiting or failed card has a ✓: seen, which clears it back to idle (a card
  whose session has gone then leaves the list). How the states
  are worked out is [../docs/status.md](../docs/status.md); the window runs
  the pane monitor itself unless the voice server already is.
- **The monitor** under both views: *System* is CPU (with load, and on hover
  what each project is using, from reserve/), the cores booked in reserve/'s
  queue (click for its page; hidden while reserve/ isn't up), memory, and the
  GPU and its memory from `nvidia-smi` (hidden without one), every 2 s.
  *Usage* (the `usage` feature) is each service quota bucket — Claude
  Code's 5h / 7d windows and grant, Antigravity's buckets and credits — as used, with the
  reset time and the turns and tokens behind it on hover. Amber from 75 %,
  red from 90 %.
- **Clicking an agent** opens a tab running `terminal/attach.py <project>
  <agent>`, so host and container sessions are reached exactly as the
  launcher reaches them, and one that isn't running is reported, not started.
  Clicking it again switches to that tab. Detaching (or closing the tab)
  closes the client; the session keeps running.
- **The Projects view** (the switch above the list) is every project in the
  registry, running or not, from the same `registry.status()` the hub shows,
  every 5 s, **grouped by repository**: a monorepo's worktrees sit together
  under its name, and projects outside git come last under *Not in git*.
  Each row has its branch, whether it is a worktree and which subdirectory
  of the checkout it is, chips for its Jira tickets (click to open one in a
  tab), and a control per agent offered plus (with a port) `web`; the ●
  says whether it is up. Clicking a stopped agent opens a tab running
  `start.py`, which is the registry's own `registry.launch_agent`: the
  session is created exactly as `proj agent` creates it, then attached. ■ kills the session (after asking), host or container. `web`
  brings a managed instance up with the hub's `registry.start_web` and opens
  it in a tab here; once it's up, clicking it opens (or returns to) that tab,
  and ■ is `registry.stop_web`. A web tab has back/forward/reload, its
  address, and a button that hands the page to the real browser. A bad
  certificate is accepted only for localhost and the registered services'
  hosts (and the host a project's web tab was opened on).
- **Worktrees from the window.** Each repository's heading has a **+**: a
  dialog for a new worktree of it (name, branch, base for a new branch,
  subdirectory, ticket), which runs `registry.add_worktree_project` — what
  `proj add --worktree` runs — then opens the new project's agent. A
  worktree project with no agent running has a 🗑 that removes it and its
  checkout (the branch is kept), refused if it has uncommitted changes.
- **The Tickets view** (with `[jira]` configured; see
  [../core/README.md](../core/README.md#jira)) is the config's JQL — your
  open tickets by default — with each one's status and the projects linked
  to it, refreshed every two minutes and on ⟳. Clicking a ticket gives its
  menu: open it in a tab or the browser; **start work in a new worktree**
  (the worktree dialog, prefilled with a branch and name from the ticket,
  linked to it, agent started); **send to agent** — its summary,
  description and link pasted as a prompt into a linked project's running
  agent, after asking; link to a project, or unlink an explicit link (a link
  that comes from a branch name goes with the branch); move it through its
  workflow; comment; assign to yourself. **+** opens a new ticket, optionally
  linked to a project. Jira and git run off the main loop; errors land in
  the status line at the top right.
- **A plain terminal** (Terminal, Ctrl+T) is a login shell and nothing else,
  in the directory of the terminal you were in — this is a vanilla terminal
  too. Every tab drops the SSH login variables (`SSH_CONNECTION` and the
  rest), so a window started from the phone's SSH login doesn't run the
  launcher in its shells.
- **A launcher tab** (Launcher, Ctrl+Shift+T; the "launcher" feature) is
  `bash -li` with `SSH_CLIENT_WEB=1`, the same way in as SSH and the browser
  terminal.
- **A free terminal names itself.** Once a launcher tab (or a plain shell
  that ran tmux) is attached to a project's session — found by its tty in
  `tmux list-clients`, or for a container by the `docker exec … tmux attach`
  the launcher runs — the tab takes the project's name, joins its cluster,
  and is the tab that clicking that agent switches to. Back at the shell
  prompt it is named for its directory again; at the launcher's menu,
  `launcher`.
- **A project's tabs stay together.** Its agents, its web app and anything
  opened from them go in beside its other tabs, and share a coloured dot —
  each open project gets a colour no other open project has.
- **Links open here.** Ctrl+click a link in a terminal (an OSC 8 hyperlink
  or any `http(s)://` text) to open it in a tab beside it, Ctrl+Shift+click
  to send it to the real browser; right-click one for both and Copy link. In
  a web tab, a link that opens a new window (`target=_blank`,
  `window.open`) or is Ctrl- or middle-clicked becomes a new tab, and the
  context menu adds *Open Link in the Browser*. The button at the end of the
  address bar pops the page out to the browser.
- **The Radio Room tab** (the "voice" feature) is voice/'s own page in WebKit, for everything the
  list doesn't do (voice, the event detail, Sonnet summaries). Its
  self-signed certificate is accepted for localhost only.
- **It follows the voice operator.** voice/'s `GET /api/desk` is read with
  the agent list: when the operator opens a project, starts an agent or
  brings a web app up, the window opens (or switches to) that tab, and going
  back to the projects view returns it to the Radio Room tab. Only what happens
  after the window starts is followed. ■ here and `stop_agent` there are the
  same `registry.stop_agent`.

Ctrl+Shift+C / V copy and paste; Ctrl+Shift+W closes a tab. Ctrl+V reaches
the agent, and Claude Code pastes an image from the clipboard with `xclip`.
An agent started before the last login still holds that login's X cookie, so
`install.sh` links `core/bin/xclip` into `~/.local/bin`: it finds the running
Xwayland's display and cookie and runs the real xclip with them.

**Tabs come back after a restart.** The open tabs, in order, are written to
`~/.local/state/radio-room-desktop/tabs.json` whenever they change, every 10 s,
and on close, and reopened at start:

- an agent tab reattaches to its session, unless that session has stopped
  since, in which case it is left out rather than opened onto an error;
- a plain terminal reopens in its directory, and a tab opened from a link at
  its page, in its project's cluster;
- a launcher tab that has since attached to a host tmux session comes back
  as that session (found by its tty in `tmux list-clients`, as terminal/
  names its tabs); one still at the launcher comes back as a launcher;
- a web tab comes back at the page it was on.

Nothing is killed by closing the window: terminals detach, so the sessions
are where they were when it reopens.

`TMUX` is dropped from every terminal's environment (VTE only adds to the
window's own environment, so it goes through `env -u`): started from inside
tmux, the window would otherwise pass it on and every attach would refuse to
nest.

GTK 3, VTE 2.91 and WebKit2 4.1, because those are the bindings installed
here; GTK 4 would want `gir1.2-vte-3.91` and `gir1.2-webkit-6.0`.

`net.radioroom.Desktop.desktop` puts it in the app grid, and the icon in
`icons/` gives it (and the dock) its eye-on-a-terminal. Both are named for
the application id, which is how GNOME on Wayland ties the running window to
its launcher:

    ln -s "$PWD/desktop/net.radioroom.Desktop.desktop" ~/.local/share/applications/
    ln -s "$PWD/desktop/icons/net.radioroom.Desktop.svg" ~/.local/share/icons/hicolor/scalable/apps/

Tabs carry an icon for what they are: a terminal, a web page, or (the one
that can't be closed) Radio Room's own page.

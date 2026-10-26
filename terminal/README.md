# terminal

A browser terminal into this machine — the same login shell an SSH login
gets, and any agent's tmux session one tap away — reachable from any device on
the tailnet without an SSH client. It needs [Tailscale](#security).

    proj add terminal ./terminal    # once: registers it and gives it a port
    proj up terminal                # proj url terminal says where

Then set `[voice] terminal_project = "terminal"` so the agent cards' Terminal
buttons open it.

## Why this and not ttyd

ttyd is fine until the connection drops.
It gives each browser its own short-lived process, so a locked phone or a walk
out of wifi range means coming back to a brand new launcher. Here the PTY lives
in the server and outlives every connection: reattaching lands on the session
exactly where it was, mid-project and mid-tmux.

The other reason is the front end. The terminal is the whole page, and getting
that to feel right on a phone — where the page wants to bounce, the keyboard
wants to shove the layout around, and a drag over a canvas does nothing at all
— takes more control than a stock client offers.

## How it fits together

```
browser ── WebSocket ── server.py ── TabSet ─┬─ PtySession ── bash -li ── (your launcher)
           binary: pty bytes                 │   history + fanout    (via ~/.bashrc)
           text:   json control              ├─ PtySession ── bash -li ── tmux ── api
                                             └─ …            one per tab
```

- **`server.py`** — stdlib HTTP server for `static/`, plus the `/ws` upgrade.
  Binds the Tailscale address by default.
- **`session.py`** — the PTY, its history, and the reattach logic.
- **`tabs.py`** — the set of those, and where their names come from.
- **`mic.py`** — the virtual microphone that browser audio is played into.
- **`wsproto.py`** — enough RFC 6455 to carry a terminal. No dependencies, to
  match the rest of Radio Room.
- **`static/`** — the page, and a vendored copy of xterm.js (`vendor/VERSIONS.txt`
  says how to refresh it).

The shell is `bash -li`, which is exactly what sshd starts. It reads
`~/.profile` and `~/.bashrc`, so **the browser and an SSH login are one code
path**. The server sets `SSH_CLIENT_WEB=1` in it.

**The launcher** is optional and yours: a project menu (anything that
ends in `tmux attach`, `proj agent` being the simplest) that your `~/.bashrc`
starts when it sees `SSH_CLIENT_WEB` — and, if you like, when an SSH login
arrives from your phone. Without one, a tab is a plain login shell and
everything below that mentions the launcher means that shell. The desktop
window's launcher tab (feature `launcher`) is the same idea. A tab running
anything called `launcher.py` is named `launcher`. Quitting the launcher drops
to the interactive shell either way.

`TMUX` is stripped from the PTY's environment, because `proj up` starts this
server inside a tmux session and an inherited `TMUX` would make the launcher's
`tmux attach` refuse to nest.

## Tabs

One PTY was the whole design until it wasn't: a phone that can only hold one
project at a time makes you quit out of one to glance at another, and quitting
is the one thing that loses your place on screen. So there is a strip of tabs
below the key row, each of them a session in its own right.

Nothing opens a tab for you. A new one starts where every session starts — at
the launcher — and you pick its project from there, which is the same handful
of keystrokes as picking one anywhere else. Closing a tab detaches rather than
kills, so the project behind it keeps running and is waiting where you left it
the next time you open it. The last tab stays: there is always a terminal.

Nothing *names* a tab either. The name is read back off the terminal a couple
of times a second, and it is whatever is in front of it:

| what is on the tab's PTY | what the tab is called |
|---|---|
| a tmux client | the session it is attached to, which is the project |
| the launcher | `launcher` |
| anything else | what the command is called — `shell`, `claude` |

That the name is a reading and not a record is what makes it right without
anything having to keep it so. Picking a project renames its tab; `⏏` back to
the launcher renames it again; a tab renamed on your phone is renamed on your
laptop, because neither of them was ever the thing that knew. Finding the tmux
session behind a tab is the same `client_tty` lookup `⏏` uses to detach one, so
the two can never disagree.

The launcher is the awkward case. `~/.bashrc` starts it before bash has set up
job control, so it never gets a process group of its own, and asking the tty
which group is in the foreground answers `bash`. Hence the walk down through
the children that stayed in the same group, which reaches exactly as far as
"the same foreground job" does.

Which tab is in front is the server's state, not the browser's, for the same
reason the PTY is: a phone and a laptop pointed at this are two views of one
thing, and switching tabs on either switches both. Only the tab in front is
subscribed to. The rest keep running and keep filling their own history, and
coming back to one replays it — the same path, and the same code, as coming
back to a phone that fell asleep.

## Opening a tab on one agent

    https://<host>:<port>/?attach=render&agent=claude

is the link Radio Room's agent cards (`../voice`) open. If a tab is already
showing that session it comes to the front; otherwise a new tab attaches to
it. The attaching is `attach.py`, which calls the launcher's own
`registry.launch_agent`, so a session inside a docker container is reached the
same way `proj agent` reaches it. It never starts an agent: an
unknown project, or one whose session isn't running, is said so on the page
and no tab is opened.

The tab runs that attach from `bash -lc`, a login shell that is not
interactive, so `~/.bashrc` returns before it would start the launcher; when
you detach, it becomes the usual `bash -li` and the launcher comes up as in
any tab. Restarting an ended tab gives a plain launcher tab, not the attach
again. The query string is dropped from the address bar once read, so a
reload is only a reload.

## Reattaching

Replaying raw output rebuilds a shell's scrollback faithfully, but a TUI's
output is a stream of patches to a screen the new browser has never seen. So
history is kept in two buffers — main-screen and alt-screen — and a reattach
resizes the PTY in a way that forces a full repaint *before* taking the
snapshot. Rebuilding the screen costs about 30 KB instead of the ~300 KB a
straight replay took, and there is no flash of stale frame first.

Switching to a tab goes through all of this unchanged, because a tab you have
not looked at in an hour and a phone that has been in your pocket for one are
the same problem.

## The terminal experience

The page is not a document and does not behave like one:

- Nothing scrolls but the terminal. `<body>` is fixed with `overscroll-behavior:
  none`, so there is no rubber-banding and no way for a scroll to grab the page.
  Reaching either end of the scrollback stops there.
- Touch scrolling is implemented here, with a glide. xterm 6 draws to a canvas
  and moves the view itself, so its viewport is not something the browser can
  pan — without this, a drag does nothing.
- A scroll gesture goes wherever a wheel would, which is three different places
  (see *Where a scroll goes* below).
- Layout follows `visualViewport`, so the soft keyboard shortens the terminal
  instead of pushing it off-screen.
- Pinch resizes the text and the grid reflows; ctrl+wheel does the same on a
  desktop. The size is remembered.
- The tab strip is always there, with or without the key row above it: it is
  structure rather than a touch affordance, and a desktop needs to know which
  project it is looking at as much as a phone does. Unlike the key row it
  scrolls, because a key row that slides under your thumb is worse than a
  cramped one — you reach for those without looking — while a project name you
  cannot read is no name at all. Tapping a tab does not take focus, so the soft
  keyboard stays where you left it: the strip refuses the default only on the
  mouse events, which browsers synthesise after a touch has been judged not to
  be a scroll, so panning still works.
- The key row (⏏, esc, tab, ⇧tab, ctrl, arrows, ⏎, ▼) appears only on touch
  devices, at a size that sits with the terminal rather than shouting over it.
  `ctrl` is sticky — tap it, then the next key — and combines with the
  arrows. The keys never take focus and never restore it, so the soft keyboard
  stays exactly where you left it: a `<button>` that takes focus on tap blurs
  the terminal and drops the keyboard, and putting focus back afterwards just
  animates it away and back on every arrow press. Input goes over the socket
  rather than through the textarea, so the keys work regardless of focus. `▼`
  is the one that dismisses the keyboard, because you asked it to.
- `⏎` is on the row because the keyboard's own return key is a thumb trip away
  from it. It empties xterm's textarea on the way, which is what xterm does
  when a real return passes through — without it the keyboard would keep
  correcting against a line the shell has already run.
- Disconnects are quiet. A brief drop reconnects without saying anything; only
  a lasting one puts a small note at the bottom, over a terminal you can still
  read.

## Where a scroll goes

A terminal scroll is not one thing, and sending the wrong one is what makes a
scroll walk through a menu instead of scrolling. The gesture is routed the same
way a wheel is:

| what is on screen | where the scroll goes |
|---|---|
| Something that asked for the mouse — tmux with `mouse on`, or claude | a wheel report, encoded by xterm for whichever protocol the app negotiated |
| A full-screen app that did not ask for the mouse | cursor keys, capped, no glide — there is no scrollback to move |
| An ordinary shell | xterm's own scrollback |

This depends on tmux asking for the mouse. Set `mouse on` in `~/.tmux.conf`,
and a generous `history-limit` (50000, say); without them tmux never requests mouse
reporting, so a scroll inside tmux falls all the way through to cursor keys and
there is no history worth reaching anyway. Scrolling down out of copy-mode
leaves it, so you get the keyboard back without pressing `q`.

Inside tmux the two upper rows both apply: tmux takes the wheel, and hands it
on to a pane whose application wants the mouse itself. So scrolling in a plain
pane moves tmux's scrollback, and scrolling in a claude pane scrolls claude's
transcript.

## Copy and paste

`mouse on` has a cost: a drag selects in tmux's copy-mode, not in the
terminal, so the terminal has nothing of its own to copy. Releasing the drag
copies it twice over, from `~/.tmux.conf`:

- `copy-command` puts it on this machine's desktop clipboard whatever terminal
  is attached — `copy-command "wl-copy"` (Wayland) or
  `copy-command "xclip -selection clipboard"` (X11). That covers VTE (the
  desktop window, GNOME Terminal), which ignores OSC 52.
- `set-clipboard on` sends it as OSC 52, which `app.js` writes to the viewing
  device's clipboard, so a copy on the phone lands on the phone.

Paste is the terminal's own: ctrl+shift+V in the desktop window, the browser's
paste in the web terminal, or the `paste` key on a touch screen. Shift+drag
skips tmux and selects in the terminal itself, for a plain copy of what's on
screen.

An image can't be pasted as bytes, and the agent can't see this device's
clipboard, so it is uploaded instead: `POST /api/image` saves it under
`~/.cache/radio-room/pastes/` (`../core/pastes.py`) and the page pastes the
saved path, which Claude Code turns into an attached image. It comes from a
browser paste or a drop on the terminal, a long-press paste into the paste
sheet, or the sheet's **image…** button (photos, camera, files). The path is
this machine's, so an agent inside a container can't open it; the voice page's
terminal copies the image into the container for those.

## Leaving a session

`⏏` on the touch key row, or **ctrl+shift+L** on a keyboard, takes the tab in
front back to the launcher. It detaches rather than kills: `tmux detach-client` against this
PTY's tty, so the project's session keeps running and picking it from the
launcher again lands exactly where you left it. If whatever is running is not
a tmux client, it falls back to SIGHUP — which a tmux client would also treat
as a detach, so the session survives either way.

## Voice mode

Claude Code's voice mode records from the server's default microphone, which
is nowhere near the phone in your hand. Holding **🎤** (right end of the tab
strip) closes that gap:

```
phone mic ── AudioWorklet ── ws binary ── pacat ── null sink ── remap-source
            s16le, native rate                                  "ssh_client mic"
                                                                 = default source
                                                                   while held
```

- The browser streams its microphone up the same WebSocket as everything else,
  as binary frames. PipeWire does the resampling; Claude asks it for 16 kHz.
- While anyone holds the button, the server makes `ssh_client_mic` the default
  source. Letting go puts the previous default back four seconds later —
  changing the default moves the streams that follow it, and Claude is still
  recording the end of the sentence when your thumb comes up.
- The held button is a held space bar as far as Claude can tell: it sends
  space at key-repeat rate, which is what Claude's hold-to-talk listens for.
  So it is for `/voice hold` (the default), and it types spaces into whatever
  else is in front.
- The `mic` message is handled before the next one is read, so the default has
  been swapped before the first space reaches Claude and it opens the device.
- The microphone stays open for 30 s after a press, so the next one does not
  lose its first word to start-up; after that the phone's mic indicator goes out.

Browsers only hand the microphone to a secure page, so the server serves HTTPS
with the machine's tailnet certificate from `tailscale cert`, cached under
`certs/` and re-fetched daily. Plain HTTP still answers on the same port —
the first byte of a TLS connection is a handshake record, nothing HTTP sends
starts with one — and page loads are redirected to
`https://<machine>.ts.net:<port>/`. `--no-tls` turns that off, and voice with it.

The PipeWire modules load on first use and unload on shutdown (SIGTERM and
SIGHUP included). A server that died hard leaves them; the next one adopts
them by name.

## Security

This serves an **unauthenticated shell**. It binds `tailscale0` only — not
localhost, not the LAN — and that binding is the entire boundary. `--bind any`
exists for a trusted network and should be treated as a decision, not a flag.

The microphone is the same boundary: anything on the tailnet that can reach
the page can talk into this machine's default source while it holds the button.

## Tests

    python3 -m pytest terminal/

Covers the handshake against the RFC 6455 vector (a wrong GUID still returns a
plausible 101 that every browser silently refuses), frame decoding, the
main/alt history split including a mode switch torn across two reads, and a
live PTY through resize, exit and restart, the tab list through opening,
switching and closing, and the naming — including the descent past the shell
that finds the launcher.

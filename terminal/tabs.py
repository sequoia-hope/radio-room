"""The tabs, and what each one is showing.

A tab is one PTY — one terminal's worth of session — and there are exactly as
many as you have opened. Nothing here opens one on your behalf: the tabs are
yours to add and close, and a new one starts where every session starts, at
the launcher.

What a tab is *called* works the other way round. No one names a tab; it is
named after whatever is currently in front of it, read back off the terminal
a couple of times a second:

    tmux client on this tty  →  the tmux session, which is the project name
    the launcher             →  "launcher"
    anything else            →  what the command is called

So picking a project in a fresh tab renames it, and quitting that project back
to the launcher renames it again, with nothing in either path having to know
that tabs exist. The name is a reading, not a record — which is also why it
survives a browser that was never there to see the change.

The PTYs themselves outlive browsers exactly as they did when there was only
one of them. Closing a tab detaches rather than kills, so the project behind
it keeps running and is waiting where you left it the next time you pick it.
"""

import os
import subprocess
import threading
import time

# Enough for every project you would plausibly hold open at once, and a ceiling
# on how many login shells one absent-minded thumb can leave running.
MAX_TABS = 12

# Names are read, not reported, so they have to be re-read. Fast enough that
# picking a project renames its tab while you are still looking at it; slow
# enough to be invisible. Only runs while a browser is connected.
POLL_INTERVAL = 1.5

SHELLS = {"bash", "sh", "zsh", "dash", "fish", "ksh"}

# What a tab is called before the first reading comes back, which is the width
# of one poll at most.
UNKNOWN = "…"


def tmux_clients() -> dict:
    """tty → tmux session name, for every attached client on this machine.

    One call covers every tab, which is why the naming poll costs one process
    regardless of how many tabs are open. tmux names clients by their tty, and
    a tab's tty is the PTY it spawned — the same handle `_detach_tmux` uses to
    let go of one.
    """
    try:
        done = subprocess.run(
            ["tmux", "list-clients", "-F", "#{client_tty}\t#{session_name}"],
            capture_output=True, timeout=3, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if done.returncode != 0:
        return {}   # no server running: nothing is attached to anything
    found = {}
    for line in done.stdout.splitlines():
        tty, sep, name = line.partition("\t")
        if sep and name:
            found[tty] = name
    return found


def name_from_chain(chain: list) -> str:
    """What to call a terminal running this, when tmux is not the answer.

    `chain` is the foreground job, outermost first — `[bash, launcher.py]`.
    The launcher wins wherever it appears in it, because that is the one thing
    here with a name of its own. Otherwise the answer is the outermost thing
    that is not a shell: a program's own helper processes are still that
    program, and naming a tab after them would make it flicker.
    """
    for argv in chain:
        # Checked across the whole argv because the launcher runs under an
        # interpreter and so is never argv[0].
        if any(arg.endswith("launcher.py") for arg in argv):
            return "launcher"
    for argv in chain:
        # A login shell introduces itself as "-bash"; a path is not a name.
        command = os.path.basename(argv[0]).lstrip("-")
        if command and command not in SHELLS:
            return command
    return "shell" if chain else UNKNOWN


class Tab:
    """One PTY, plus the last thing we read about what it is showing."""

    def __init__(self, tab_id: str, session):
        self.id = tab_id
        self.session = session
        self.name = UNKNOWN

    def read_name(self, clients: dict) -> str:
        if not self.session.alive:
            return "ended"
        attached = clients.get(self.session.tty)
        if attached:
            return attached
        return name_from_chain(self.session.foreground_chain())

    def state(self) -> dict:
        return {"id": self.id, "name": self.name, "alive": self.session.alive}


class TabSet:
    """Every open tab, which one is in front, and who to tell when that moves.

    Which tab is in front is server state rather than browser state, for the
    same reason the PTY is: a phone and a laptop looking at this are two views
    of one thing, and a tab switch on either should be a tab switch on both.
    """

    def __init__(self, spawn):
        self._spawn = spawn
        self._tabs = []
        self._by_id = {}
        self._counter = 0
        self.active = None
        # Reentrant: a mutation refreshes names, and both want the list.
        self._lock = threading.RLock()
        self._listeners = set()

        self.open()
        threading.Thread(target=self._poll, daemon=True, name="tab-names").start()

    # ── reading the list ─────────────────────────────────────────────────────

    def all(self) -> list:
        with self._lock:
            return list(self._tabs)

    @property
    def active_tab(self):
        with self._lock:
            return self._by_id.get(self.active)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "t": "tabs",
                "tabs": [tab.state() for tab in self._tabs],
                "active": self.active,
                "max": MAX_TABS,
            }

    # ── changing it ──────────────────────────────────────────────────────────

    def open(self, *spawn_args):
        """A new tab, in front, sitting at the launcher — or running whatever
        `spawn_args` ask the factory for. None if you have enough."""
        with self._lock:
            if len(self._tabs) >= MAX_TABS:
                return None
            self._counter += 1
            tab = Tab(f"t{self._counter}", self._spawn(*spawn_args))
            self._tabs.append(tab)
            self._by_id[tab.id] = tab
            self.active = tab.id
        self.refresh(announce=True)
        return tab

    def named(self, name):
        """A live tab currently showing `name`, if there is one."""
        with self._lock:
            return next((t for t in self._tabs
                         if t.name == name and t.session.alive), None)

    def select(self, tab_id):
        with self._lock:
            tab = self._by_id.get(tab_id)
            if tab is None or self.active == tab_id:
                return tab
            self.active = tab_id
        self.refresh(announce=True)
        return tab

    def close(self, tab_id) -> bool:
        """Let a tab go. The last one stays: there is always a terminal."""
        with self._lock:
            tab = self._by_id.get(tab_id)
            if tab is None or len(self._tabs) <= 1:
                return False
            index = self._tabs.index(tab)
            self._tabs.remove(tab)
            del self._by_id[tab_id]
            if self.active == tab_id:
                # The one that slid into its place, or the new last one.
                self.active = self._tabs[min(index, len(self._tabs) - 1)].id
        # Outside the lock: detaching a tmux client takes a moment, and the
        # rest of the list should not wait on it.
        tab.session.stop()
        self.refresh(announce=True)
        return True

    # ── names ────────────────────────────────────────────────────────────────

    def refresh(self, announce: bool = False) -> None:
        """Re-read what every tab is showing, and say so if anything moved."""
        clients = tmux_clients()   # one process for the whole list, no lock held
        with self._lock:
            changed = False
            for tab in self._tabs:
                name = tab.read_name(clients)
                if name != tab.name:
                    tab.name = name
                    changed = True
        if changed or announce:
            self.publish()

    def _poll(self) -> None:
        while True:
            time.sleep(POLL_INTERVAL)
            # Nobody is watching, so nobody needs the answer; an idle server
            # should not be spawning a tmux process forever.
            if self._listeners:
                self.refresh()

    # ── telling the browsers ─────────────────────────────────────────────────

    def listen(self, send) -> None:
        with self._lock:
            self._listeners.add(send)

    def unlisten(self, send) -> None:
        with self._lock:
            self._listeners.discard(send)

    def publish(self) -> None:
        state = self.snapshot()
        with self._lock:
            listeners = list(self._listeners)
        for send in listeners:
            send(state)

"""The PTY behind the web terminal.

The session outlives any browser connection. Closing the tab, locking the
phone or dropping off wifi leaves the launcher — and whatever project it
handed off to — running exactly where it was; the next connection reattaches
to the same PTY and replays recent output to rebuild the screen.

That is the whole reason this is a bespoke server and not `ttyd`: ttyd hands
each browser its own short-lived process, so a dropped connection means a
fresh launcher every time.
"""

import errno
import fcntl
import os
import pty
import re
import signal
import struct
import subprocess
import termios
import threading
import time
import warnings

# Shell scrollback: what a reconnecting browser needs to see above the fold.
MAIN_BYTES = 256 * 1024

# Alt-screen output is kept separately. A reattach clears it and makes the app
# repaint, so in practice this holds exactly one full frame — generous, because
# a frame of a big truecolor TUI is not small.
ALT_BYTES = 512 * 1024

READ_CHUNK = 65536

# How far down a foreground job to look for the process actually on screen.
# Two is the common case (a shell and the thing it is running); the rest is
# headroom, and a bound so this can never become a walk of the process table.
FOREGROUND_DEPTH = 6

# Entering the alternate screen ends scrollback and starts a screen that is
# redrawn wholesale, which is exactly the boundary the two buffers split on.
ALT_MODE_RE = re.compile(rb"\x1b\[\?(?:1049|1047|47)([hl])")
ALT_SEQUENCES = (b"\x1b[?1049h", b"\x1b[?1049l", b"\x1b[?1047h",
                 b"\x1b[?1047l", b"\x1b[?47h", b"\x1b[?47l")
ENTER_ALT = b"\x1b[?1049h"


def partial_mode_suffix(data: bytes) -> int:
    """Length of a trailing fragment that could still become a mode sequence.

    A PTY read can land mid-escape. Splitting a mode switch across the two
    history buffers would leave a dangling fragment in one of them that eats
    the next character on replay, so those bytes wait for the rest to arrive.
    """
    for length in range(min(7, len(data)), 0, -1):
        fragment = data[-length:]
        if any(seq.startswith(fragment) for seq in ALT_SEQUENCES):
            return length
    return 0


def set_winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def process_argv(pid: int) -> list:
    """argv of one process, or [] if it has gone or will not say."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        return []
    return [arg.decode("utf-8", "replace") for arg in raw.split(b"\0") if arg]


def group_child(pid: int, pgid: int):
    """A child of `pid` that is in process group `pgid`, if there is one.

    Same group means same foreground job, which is the line between "this is
    what the terminal is showing" and "this is something it started and left
    to run".
    """
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as handle:
            children = handle.read().split()
    except OSError:
        return None
    for child in children:
        try:
            with open(f"/proc/{child}/stat") as handle:
                # comm is parenthesised and may contain spaces, so the fields
                # are only reliably positional after the last ')'.
                fields = handle.read().rpartition(")")[2].split()
            if int(fields[2]) == pgid:   # state, ppid, pgrp
                return int(child)
        except (OSError, IndexError, ValueError):
            continue
    return None


class PtySession:
    """One long-lived PTY plus the tail of everything it has printed."""

    def __init__(self, argv, env, cwd, cols=80, rows=24, restart_argv=None):
        self.argv = argv
        # A tab opened on one project still restarts to the launcher.
        self.restart_argv = restart_argv
        self.env = env
        self.cwd = cwd
        self.cols = cols
        self.rows = rows

        self.pid = None
        self.fd = None
        self.tty = None
        self.exit_code = None

        self._main = bytearray()      # scrollback, alt-screen output excluded
        self._alt = bytearray()       # output since entering the alt screen
        self._alt_mode = False
        self._pending = b""           # a mode sequence split across two reads
        self._lock = threading.Lock()
        self._subscribers = set()

        self.spawn()

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def alive(self) -> bool:
        return self.pid is not None and self.exit_code is None

    def spawn(self) -> None:
        if self.alive:
            return

        master, slave = pty.openpty()
        # Size the slave before the fork so the child's very first paint is
        # already the right shape — no visible reflow on startup.
        set_winsize(slave, self.cols, self.rows)

        with warnings.catch_warnings():
            # Python warns that forking a threaded process can deadlock the
            # child. True in general, and the reason the child branch below is
            # nothing but fd setup and an exec — the same shape as pty.fork().
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:
            # Child. Keep this path allocation-light: fork() in a threaded
            # process only guarantees the calling thread survives.
            try:
                os.close(master)
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
                os.dup2(slave, 0)
                os.dup2(slave, 1)
                os.dup2(slave, 2)
                if slave > 2:
                    os.close(slave)
                os.chdir(self.cwd)
                os.execve(self.argv[0], self.argv, self.env)
            except BaseException:
                os._exit(127)

        self.tty = os.ttyname(slave)
        os.close(slave)
        self.pid = pid
        self.fd = master
        self.exit_code = None
        with self._lock:
            self._main.clear()
            self._alt.clear()
            self._alt_mode = False
            self._pending = b""

        threading.Thread(target=self._pump, daemon=True, name="pty-read").start()

    def _detach_tmux(self) -> bool:
        """Ask tmux to let go of this PTY, if that is what is holding it.

        The launcher execs a project's launch.sh, which execs `tmux attach` —
        so most of the time the process on this PTY *is* a tmux client, and
        detaching it is the graceful way out: the client exits, the project's
        session keeps running, and going back into it from the launcher picks
        up exactly where it was. tmux names clients by their tty.
        """
        if not self.tty:
            return False
        try:
            done = subprocess.run(
                ["tmux", "detach-client", "-t", self.tty],
                capture_output=True, timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if done.returncode != 0:
            return False
        deadline = time.monotonic() + 2.0
        while self.alive and time.monotonic() < deadline:
            time.sleep(0.02)
        return not self.alive

    def stop(self) -> None:
        """Let go of whatever is running, without starting anything after it.

        Detaching rather than killing is the whole point: what is usually on
        this PTY is a tmux client, and the project behind it should outlive
        both the client and this terminal.
        """
        if not self.alive or self._detach_tmux():
            return
        # Not a tmux client, or tmux would not let go. SIGHUP is what a
        # closing terminal sends, and a tmux client that somehow got here
        # still treats it as a detach rather than dying with the session.
        try:
            os.killpg(os.getpgid(self.pid), signal.SIGHUP)
        except OSError:
            pass
        deadline = time.monotonic() + 2.0
        while self.alive and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.alive:
            try:
                os.killpg(os.getpgid(self.pid), signal.SIGKILL)
            except OSError:
                pass
            while self.alive:
                time.sleep(0.02)

    def restart(self) -> None:
        """Leave whatever is running and come back to a fresh launcher."""
        self.stop()
        if self.restart_argv:
            self.argv = self.restart_argv
        self.spawn()

    def foreground_chain(self) -> list:
        """argv of everything in the process group that owns the terminal.

        Which is to say: what you are looking at, and what it is running under.
        A tab reads its name off this rather than being told one, so a tab that
        quits its project back to the launcher renames itself without anyone
        having to report the change.

        The list is a chain, outermost first — `[bash, launcher.py]`. One entry
        would not be enough: the launcher is started from ~/.bashrc, before
        bash has job control, so it never gets a process group of its own and
        tcgetpgrp() names the shell rather than the thing on the screen. So
        follow the children down as long as they stay in the same group, which
        is exactly as far as "the same foreground job" reaches.

        Empty when there is nothing to read — the group leader has already
        exited, or /proc will not say.
        """
        if not self.alive:
            return []
        try:
            # The master side answers this as readily as the slave does, and
            # the master is the end we still hold.
            pgid = os.tcgetpgrp(self.fd)
        except OSError:
            return []

        chain = []
        pid = pgid
        # A foreground job is a handful of processes deep at most; the bound is
        # only here so a surprise can never turn this into a walk.
        for _ in range(FOREGROUND_DEPTH):
            argv = process_argv(pid)
            if argv:
                chain.append(argv)
            pid = group_child(pid, pgid)
            if pid is None:
                break
        return chain

    def _pump(self) -> None:
        """Read the PTY forever, keeping history and fanning out to browsers."""
        fd = self.fd
        while True:
            try:
                data = os.read(fd, READ_CHUNK)
            except OSError as e:
                # EIO is how a PTY master reports "the child let go".
                if e.errno in (errno.EIO, errno.EBADF):
                    data = b""
                else:
                    raise
            if not data:
                break

            with self._lock:
                self._record(data)
                subscribers = list(self._subscribers)
            for send in subscribers:
                send(data)

        code = 0
        try:
            _, status = os.waitpid(self.pid, 0)
            code = os.waitstatus_to_exitcode(status)
        except (ChildProcessError, ValueError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass

        self.exit_code = code
        self.pid = None
        self.fd = None
        self.tty = None
        with self._lock:
            subscribers = list(self._subscribers)
        for send in subscribers:
            send(None)  # sentinel: the session ended

    # ── history ──────────────────────────────────────────────────────────────

    def _store(self, chunk: bytes) -> None:
        """File a run of output under whichever screen is current."""
        if not chunk:
            return
        if self._alt_mode:
            self._alt += chunk
            if len(self._alt) > ALT_BYTES:
                del self._alt[: len(self._alt) - ALT_BYTES]
        else:
            self._main += chunk
            if len(self._main) > MAIN_BYTES:
                del self._main[: len(self._main) - MAIN_BYTES]

    def _record(self, data: bytes) -> None:
        """Split output at every alt-screen switch. Caller holds the lock.

        The switch sequences themselves are dropped rather than stored: replay
        synthesises the one it needs, so a stored `1049l` can never fire
        against a screen that was never entered and restore a cursor that was
        never saved.
        """
        buf = self._pending + data
        self._pending = b""
        cursor = 0
        for match in ALT_MODE_RE.finditer(buf):
            self._store(buf[cursor : match.start()])
            entering = match.group(1) == b"h"
            if entering:
                self._alt.clear()
            self._alt_mode = entering
            cursor = match.end()

        tail = buf[cursor:]
        hold = partial_mode_suffix(tail)
        if hold:
            self._pending = bytes(tail[len(tail) - hold :])
            tail = tail[: len(tail) - hold]
        self._store(tail)

    def _replay(self) -> bytes:
        """What a fresh terminal must be fed to look like this session.

        Scrollback first, then — if an app currently owns the screen — the
        switch into it and the frame it drew for us in repaint().
        """
        if self._alt_mode:
            return bytes(self._main) + ENTER_ALT + bytes(self._alt)
        return bytes(self._main)

    # ── browser-facing ───────────────────────────────────────────────────────

    def subscribe(self, send) -> bytes:
        """Register an output sink and get the history to replay, atomically.

        Taking both under one lock is what stops a reconnecting browser from
        missing bytes written between the history snapshot and the sink going
        live, or from seeing them twice.
        """
        with self._lock:
            self._subscribers.add(send)
            return self._replay()

    def unsubscribe(self, send) -> None:
        with self._lock:
            self._subscribers.discard(send)

    def write(self, data: bytes) -> None:
        if not self.alive:
            return
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        cols = max(2, min(1000, int(cols)))
        rows = max(2, min(500, int(rows)))
        self.cols, self.rows = cols, rows
        if self.alive:
            try:
                set_winsize(self.fd, cols, rows)
            except OSError:
                pass

    def repaint(self, cols: int, rows: int) -> None:
        """Resize to a reattaching browser, arranging a full redraw as we go.

        Replayed history reconstructs a shell's scrollback faithfully, but an
        alt-screen app (the launcher, tmux, claude) only ever paints what
        changed — its history is a stream of patches to a screen this browser
        has never seen. So make it repaint, and take the snapshot afterwards.

        A size change is a SIGWINCH and every TUI redraws on one, so when the
        browser brings a new size the redraw is free. When it does not, a
        one-row detour manufactures the same thing. Either way the alt buffer
        is emptied immediately before the final resize, so what it then holds
        is one clean current frame rather than that frame layered over a stale
        one — which is the difference between a reconnect that looks instant
        and one that flashes garbage first.

        Called before subscribe(), so any part of the repaint that has not
        arrived yet simply streams in behind the replay, in order.
        """
        if not self.alive:
            self.cols, self.rows = cols, rows
            return

        if (cols, rows) == (self.cols, self.rows):
            try:
                set_winsize(self.fd, cols, max(2, rows - 1))
            except OSError:
                pass
            time.sleep(0.08)

        with self._lock:
            if self._alt_mode:
                self._alt.clear()
        self.resize(cols, rows)

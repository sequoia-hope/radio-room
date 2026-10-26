#!/usr/bin/env python3
"""Radio Room on the desktop — the agent list beside real terminals.

    desktop/radio_room.py

A GTK window: on the left, every agent voice/ knows about (polled from its
/api/instances), on the right a notebook of VTE terminals and web pages.
Picking an agent opens a tab attached to its session — through
terminal/attach.py, so host and container sessions are reached the way the
launcher reaches them — or switches to the tab already showing it. A plain
terminal (Ctrl+T) is just a login shell; a launcher tab is the same login the
browser terminal and SSH land on. Radio Room's own page is a tab too, and
links opened anywhere open as tabs beside the one they came from.

The Projects view is the whole registry, running or not: each agent session
started (the launcher's way, through start.py), opened or stopped, and each
web instance brought up or down the way the hub does it. Under both views,
the machine's load and what is left of each service's quota.

GTK 3, because that is where VTE's bindings are installed here; nothing in
it is GTK-3 specific beyond the API names.
"""

import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte, WebKit2  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ATTACH = ROOT / "terminal" / "attach.py"
START = Path(__file__).resolve().parent / "start.py"
ICONS = Path(__file__).resolve().parent / "icons"
APP_ID = "net.radioroom.Desktop"   # also the icon's and the .desktop file's name
sys.path.insert(0, str(ROOT / "terminal"))
sys.path.insert(0, str(ROOT / "voice"))
sys.path.insert(0, str(ROOT / "core"))
from attach import AttachError, load_registry, resolve  # noqa: E402
import status as agent_status  # noqa: E402
import tickets  # noqa: E402
import cpu_view  # noqa: E402

registry = load_registry()
radio_config = registry.radio_config
PROJ = str(ROOT / "core" / "proj")
VOICE_PROJECT = radio_config.CFG["voice_project"]
RESERVE_PROJECT = "reserve"      # the CPU booking queue, shown in the monitor
STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") \
    / "radio-room-desktop" / "tabs.json"   # the open tabs, restored at start
POLL = 3.0                       # seconds between agent status passes
PROJECT_POLL = 5.0               # seconds between registry status passes
MONITOR_POLL = 2.0               # seconds between system samples
TITLE_POLL = 1.0                 # seconds between free terminals' title checks

# voice/ serves HTTPS with its own certificate; we only ever talk to it on
# this machine, so the certificate is not what keeps that honest.
INSECURE = ssl.create_default_context()
INSECURE.check_hostname = False
INSECURE.verify_mode = ssl.CERT_NONE

STATE_COLOURS = {
    "needs_input": "#e5a50a",
    "working": "#3584e4",
    "completed": "#2ec27e",
    "error": "#e01b24",
    "new": "#9141ac",
}

# What a terminal treats as a link, besides OSC 8 hyperlinks.
URL_REGEX = Vte.Regex.new_for_match(
    r"https?://[^\s<>\"'`]+[^\s<>\"'`.,;:!?)\]}]", -1, 0x00000400)  # PCRE2_MULTILINE

CSS = b"""
levelbar.meter trough, levelbar.meter block { min-height: 6px; border-radius: 3px; }
levelbar.meter block.filled { background-color: #3584e4; border-color: #3584e4; }
levelbar.meter.warn block.filled { background-color: #e5a50a; border-color: #e5a50a; }
levelbar.meter.crit block.filled { background-color: #e01b24; border-color: #e01b24; }
button.ticket-chip { padding: 0 4px; min-height: 0; font-size: smaller; }
.repo-header { padding: 6px 8px 2px; border-top: 1px solid alpha(currentColor, 0.15); }
.monitor { border-top: 1px solid alpha(currentColor, 0.15); }
"""


def voice_url() -> str:
    """voice/'s base URL, from the registry — never a port of our own."""
    out = subprocess.run([PROJ, "url", VOICE_PROJECT], capture_output=True,
                         text=True)
    url = out.stdout.strip()
    if out.returncode or not url:
        sys.exit(f"radio-room: `proj url {VOICE_PROJECT}` gave nothing "
                 f"({out.stderr.strip() or 'no output'})")
    return url.rstrip("/")


def clean_argv(argv: list[str], extra_env: dict) -> list[str]:
    # VTE adds the env it is given to the window's own rather than replacing
    # it, and the window may be started from inside tmux: an inherited TMUX
    # would make every attach refuse to nest, so env(1) drops it. So too
    # SSH_CLIENT_WEB, which only a launcher tab should have (env applies -u
    # before the assignments), and an SSH login's variables: ~/.bashrc runs
    # the launcher for the phone's SSH_CONNECTION, and a window started from
    # that login is still a desktop terminal.
    sets = [f"{k}={v}" for k, v in {"TERM": "xterm-256color", **extra_env}.items()]
    unset = ("TMUX", "TMUX_PANE", "SSH_CLIENT_WEB", "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
    return ["env", *(a for k in unset for a in ("-u", k)), *sets, *argv]


# The dot a project's tabs share, so they read as one cluster.
GROUP_COLOURS = ("#62a0ea", "#f6d32d", "#57e389", "#ff7800", "#c061cb",
                 "#99c1f1", "#ed333b", "#8ff0a4", "#cdab8f", "#dc8add")


def host_of(url: str) -> str | None:
    try:
        return GLib.Uri.parse(url, GLib.UriFlags.NONE).get_host()
    except GLib.Error:
        return None


def link_menu(widget, ev, url: str, on_link):
    """Right-click on a link: here, out in the browser, or copied."""
    menu = Gtk.Menu()
    for label, act in (
            ("Open in a tab", lambda *_: on_link(url, False)),
            ("Open in the browser", lambda *_: on_link(url, True)),
            ("Copy link", lambda *_: Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(url, -1))):
        item = Gtk.MenuItem(label=label)
        item.connect("activate", act)
        menu.append(item)
    menu.attach_to_widget(widget)
    menu.show_all()
    menu.popup_at_pointer(ev)
    return menu


class Terminal(Gtk.ScrolledWindow):
    """One VTE running one command; closes its tab when the command ends
    cleanly, and stays open to show why when it doesn't. Ctrl+click on a
    link opens it in a tab (Ctrl+Shift+click, in the browser); right-click
    on one offers both."""

    def __init__(self, argv: list[str], on_exit, extra_env: dict | None = None,
                 cwd: str | None = None):
        super().__init__()
        self.vte = Vte.Terminal()
        self.vte.set_scrollback_lines(10000)
        self.vte.set_font(Pango.FontDescription("Monospace 11"))
        self.vte.set_allow_hyperlink(True)
        tag = self.vte.match_add_regex(URL_REGEX, 0)
        self.vte.match_set_cursor_name(tag, "pointer")
        self.vte.connect("child-exited", self._exited)
        self.vte.connect("key-press-event", self._keys)
        self.vte.connect("button-press-event", self._click)
        self.add(self.vte)
        self.on_exit = on_exit
        self.on_link = None             # (url, out) — set by the window
        self.pid = None
        self.memo: dict | None = None   # how to reopen this tab next start
        self.group: str | None = None   # the project whose cluster it is in
        self._menu = None
        self.vte.spawn_async(Vte.PtyFlags.DEFAULT, cwd or str(Path.home()),
                             clean_argv(argv, extra_env or {}), None,
                             GLib.SpawnFlags.SEARCH_PATH, None, None, -1, None,
                             self._spawned, None)

    def _spawned(self, _vte, pid, _error, _data=None):
        self.pid = pid if pid > 0 else None

    def tty(self) -> str | None:
        """The child's terminal, /dev/pts/N — what tmux calls a client_tty.
        (env(1) execs, so the child is the shell or attach itself.)"""
        try:
            return os.readlink(f"/proc/{self.pid}/fd/0") if self.pid else None
        except OSError:
            return None

    def descendants(self) -> list[str]:
        out, todo = [], [str(self.pid)] if self.pid else []
        while todo:
            pid = todo.pop()
            try:
                for t in os.listdir(f"/proc/{pid}/task"):
                    kids = Path(f"/proc/{pid}/task/{t}/children").read_text().split()
                    out += kids
                    todo += kids
            except OSError:
                pass
        return out

    def at_prompt(self) -> bool:
        """Whether the shell is back at its prompt, running nothing."""
        return not self.descendants()

    def docker_session(self) -> str | None:
        """'container:session' while this terminal is attached to tmux in a
        container, the way registry.launch_agent attaches — out of sight of
        the host's tmux, and titled by the container's."""
        for pid in self.descendants():
            try:
                argv = [a.decode(errors="replace") for a in
                        Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
            except OSError:
                continue
            if argv[:2] == ["docker", "exec"] and "-it" in argv:
                m = re.search(r"attach-session -t '?=([^'\s]+)", argv[-1])
                if m and argv.index("-it") + 1 < len(argv):
                    return f"{argv[argv.index('-it') + 1]}:{m.group(1)}"
        return None

    def cwd(self) -> str | None:
        try:
            return os.readlink(f"/proc/{self.pid}/cwd") if self.pid else None
        except OSError:
            return None

    def title(self) -> str:
        """What a program in it last titled it (OSC 0/2)."""
        return self.vte.get_termprop_string(Vte.TERMPROP_XTERM_TITLE)[0] or ""

    def where(self) -> str:
        """Where the shell is, as ~/dir."""
        cwd = self.cwd()
        home = str(Path.home())
        return "~" + cwd[len(home):] if cwd and (cwd + "/").startswith(home + "/") else cwd or ""

    def _exited(self, _vte, status):
        if os.waitstatus_to_exitcode(status) == 0:
            self.on_exit(self)
        else:
            self.vte.feed(b"\r\n\x1b[2m[exited - close this tab with its x]\x1b[0m\r\n")

    def _keys(self, _vte, ev):
        mods = ev.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        if mods != Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK:
            return False
        key = Gdk.keyval_to_lower(ev.keyval)
        if key == Gdk.KEY_c:
            self.vte.copy_clipboard_format(Vte.Format.TEXT)
            return True
        if key == Gdk.KEY_v:
            self.vte.paste_clipboard()
            return True
        return False

    def _click(self, vte, ev):
        if ev.type != Gdk.EventType.BUTTON_PRESS or self.on_link is None:
            return False
        url = vte.hyperlink_check_event(ev) or vte.match_check_event(ev)[0]
        if not url:
            return False
        if ev.button == 1 and ev.state & Gdk.ModifierType.CONTROL_MASK:
            self.on_link(url, bool(ev.state & Gdk.ModifierType.SHIFT_MASK))
            return True
        if ev.button == 3:
            self._menu = link_menu(self, ev, url, self.on_link)
            return True
        return False


class WebPage(Gtk.Box):
    """A browser tab: back, forward, reload, the address, and a way out to
    the real browser. A certificate error is waved through only for
    `trusted` hosts — registered local services and localhost; voice/ and
    others serve HTTPS with their own certificates. A link the page opens in
    a new window, or that is Ctrl- or middle-clicked, becomes a new tab."""

    def __init__(self, url: str | None, trusted: set[str], closable=True,
                 related: WebKit2.WebView | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.closable = closable
        self.memo: dict | None = None
        self.group: str | None = None
        self.on_link = None      # (url, out) — set by the window
        self.on_new = None       # () -> WebPage, for a page's own new window
        self.on_title = None     # (page) — set by the window
        # A page opened by another shares its process and settings, so that
        # window.open() and its opener can still talk to each other.
        self.view = WebKit2.WebView(related_view=related) if related else WebKit2.WebView()
        # The browser terminal writes tmux's copies (OSC 52) with
        # navigator.clipboard, which WebKit refuses a page by default.
        self.view.get_settings().set_javascript_can_access_clipboard(True)

        def tls(view, uri, cert, _errors):
            host = host_of(uri)
            if host not in trusted:
                return False
            view.get_context().allow_tls_certificate_for_host(cert, host)
            view.load_uri(uri)
            return True

        self.view.connect("load-failed-with-tls-errors", tls)
        self.view.connect("create", self._create)
        self.view.connect("decide-policy", self._policy)
        self.view.connect("context-menu", self._context_menu)
        self.view.connect("notify::title", lambda *_: self.on_title and self.on_title(self))

        bar = Gtk.Box(spacing=4, margin=4)
        for icon, tip, act in (
                ("go-previous-symbolic", "Back", lambda *_: self.view.go_back()),
                ("go-next-symbolic", "Forward", lambda *_: self.view.go_forward()),
                ("view-refresh-symbolic", "Reload", lambda *_: self.view.reload())):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_tooltip_text(tip)
            b.connect("clicked", act)
            bar.pack_start(b, False, False, 0)
        self.address = Gtk.Entry(hexpand=True)
        self.address.connect("activate", lambda e: self.view.load_uri(e.get_text()))
        self.view.connect("notify::uri", lambda v, _p: self.address.set_text(v.get_uri() or ""))
        bar.pack_start(self.address, True, True, 0)
        out = Gtk.Button.new_from_icon_name("web-browser-symbolic", Gtk.IconSize.BUTTON)
        out.set_relief(Gtk.ReliefStyle.NONE)
        out.set_tooltip_text("Pop out to the browser")
        out.connect("clicked", lambda *_: self.on_link and self.on_link(self.view.get_uri(), True))
        bar.pack_end(out, False, False, 0)

        self.pack_start(bar, False, False, 0)
        self.pack_start(self.view, True, True, 0)
        if url:
            self.view.load_uri(url)

    def title(self) -> str:
        return self.view.get_title() or self.view.get_uri() or ""

    def _create(self, _view, _nav):
        # Returning the new page's view lets WebKit load into it itself.
        page = self.on_new() if self.on_new else None
        return page.view if page else None

    def _policy(self, _view, decision, kind):
        if kind != WebKit2.PolicyDecisionType.NAVIGATION_ACTION or self.on_link is None:
            return False
        nav = decision.get_navigation_action()
        if nav.get_navigation_type() != WebKit2.NavigationType.LINK_CLICKED:
            return False
        if nav.get_mouse_button() == 2 or nav.get_modifiers() & Gdk.ModifierType.CONTROL_MASK:
            self.on_link(nav.get_request().get_uri(), False)
            decision.ignore()
            return True
        return False

    def _context_menu(self, _view, menu, _ev, hit):
        if hit.context_is_link() and self.on_link is not None:
            uri = hit.get_link_uri()
            act = Gio.SimpleAction.new("open-in-browser", None)
            act.connect("activate", lambda *_: self.on_link(uri, True))
            menu.append(WebKit2.ContextMenuItem.new_from_gaction(
                act, "Open Link in the Browser", None))
        return False


def fmt_tokens(n: int | float | None) -> str:
    n = float(n or 0)
    if n >= 1e9:
        return f"{n / 1e9:.0f}B" if n >= 1e10 else f"{n / 1e9:.1f}B"
    if n >= 1e6:
        return f"{n / 1e6:.0f}M" if n >= 1e7 else f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k" if n >= 1e4 else f"{n / 1e3:.1f}k"
    return f"{int(n)}"


def dot(up: bool) -> str:
    return f'<span foreground="{"#2ec27e" if up else "#555555"}">●</span>'


def web_url(e: dict) -> str:
    """A registry status entry's address, as the hub links it."""
    return e["url"] or f"{e['scheme']}://{e['host'] or 'localhost'}:{e['port']}{e['path']}"


class Control(Gtk.Box):
    """A labelled ●, an action button (start or open) and a stop button."""

    def __init__(self, label: str, on_go, on_stop):
        super().__init__(spacing=0)
        self.get_style_context().add_class("linked")
        self.go = Gtk.Button()
        self.go_label = Gtk.Label()
        self.go.add(self.go_label)
        self.go.connect("clicked", lambda *_: on_go())
        self.stop = Gtk.Button.new_from_icon_name("media-playback-stop-symbolic",
                                                   Gtk.IconSize.MENU)
        self.stop.set_tooltip_text(f"Stop {label}")
        self.stop.connect("clicked", lambda *_: on_stop())
        self.pack_start(self.go, False, False, 0)
        self.pack_start(self.stop, False, False, 0)
        self.label = label

    def update(self, up: bool, can_stop: bool, go_tip: str):
        self.go_label.set_markup(f"{dot(up)} {self.label}")
        self.go.set_tooltip_text(go_tip)
        self.stop.set_visible(up and can_stop)


class Controls(Gtk.Box):
    """A project's claude, agy and web controls — in its Projects row, and
    in each of its agents' rows (less the agent the row already is)."""

    def __init__(self, win, name: str, skip: str | None = None):
        super().__init__(spacing=6)
        self.win, self.name, self.skip = win, name, skip
        self.entry: dict | None = None
        # One control per agent offered (registry.AGENTS), then the web app.
        self.agents = {a: Control(a, lambda a=a: self.agent(a),
                                  lambda a=a: win.stop_agent(name, a))
                       for a in registry.AGENTS}
        self.web = Control("web", self.web_go, lambda: win.web(name, False))
        for c in (*self.agents.values(), self.web):
            self.pack_start(c, False, False, 0)
        self.show_all()   # first, so update()'s hiding sticks
        self.set_no_show_all(True)

    def update(self, e: dict | None):
        self.entry = e
        self.set_visible(e is not None)
        if e is None:
            return
        for a, c in self.agents.items():
            st = e["agents"].get(a) or {}
            c.set_visible(bool(st.get("session")) and self.skip != a)
            c.update(st.get("up", False), True,
                     "Open its session" if st.get("up") else f"Start {a} here")
        self.web.set_visible(bool(e["port"]))
        self.web.update(e["up"], e["managed"] or e["supervised"],
                        "Open it in a tab" if e["up"] else
                        "Bring the web instance up" if e["managed"] else
                        "Not managed by the registry")
        self.web.go.set_sensitive(e["up"] or e["managed"])

    def agent(self, agent: str):
        up = self.entry["agents"][agent]["up"]
        self.win.open_agent(self.name, agent, start=not up)

    def web_go(self):
        if self.entry["up"]:
            self.win.open_web(self.name, web_url(self.entry))
        else:
            self.win.web(self.name, True)


class PinButton(Gtk.ToggleButton):
    """📌 for one project in one view ("agents" or "projects"): pinned, it
    stays at the top of that view. Faint while unpinned."""

    def __init__(self, win, project: str, view: str):
        super().__init__()
        self.add(Gtk.Image.new_from_icon_name("view-pin-symbolic", Gtk.IconSize.MENU))
        self.set_relief(Gtk.ReliefStyle.NONE)
        self.handler = self.connect("toggled", lambda b: win.pin(project, view, b.get_active()))

    def show_pinned(self, pinned: bool):
        with self.handler_block(self.handler):
            self.set_active(pinned)
        self.set_opacity(1 if pinned else 0.4)
        self.set_tooltip_text("Unpin" if pinned else "Pin to the top")


class AgentRow(Gtk.ListBoxRow):
    def __init__(self, win, inst: dict):
        super().__init__()
        self.key = (inst["project"], inst["agent"])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin=8)
        top = Gtk.Box(spacing=6)
        self.dot = Gtk.Label()
        self.name = Gtk.Label(xalign=0)
        self.name.set_ellipsize(Pango.EllipsizeMode.END)
        self.state = Gtk.Label(xalign=1)
        self.state.get_style_context().add_class("dim-label")
        # ✓ Seen: clears a finished, waiting or failed card back to idle.
        self.seen = Gtk.Button.new_from_icon_name("object-select-symbolic", Gtk.IconSize.MENU)
        self.seen.set_relief(Gtk.ReliefStyle.NONE)
        self.seen.set_tooltip_text("Seen — clear it back to idle")
        self.seen.connect("clicked", lambda *_: win.dismiss(*self.key))
        self.seen.set_no_show_all(True)
        # Pin: the project's card stays, at the top, whatever it is doing.
        self.pin = PinButton(win, inst["project"], "agents")
        top.pack_start(self.dot, False, False, 0)
        top.pack_start(self.name, True, True, 0)
        top.pack_end(self.pin, False, False, 0)
        top.pack_end(self.seen, False, False, 0)
        top.pack_end(self.state, False, False, 0)
        self.summary = Gtk.Label(xalign=0)
        self.summary.set_line_wrap(True)
        self.summary.set_lines(3)
        self.summary.set_ellipsize(Pango.EllipsizeMode.END)
        self.summary.set_max_width_chars(36)
        self.summary.get_style_context().add_class("dim-label")
        self.controls = Controls(win, inst["project"], skip=inst["agent"])
        box.pack_start(top, False, False, 0)
        box.pack_start(self.summary, False, False, 0)
        box.pack_start(self.controls, False, False, 2)
        self.add(box)
        self.show_all()
        self.update(inst)
        self.controls.update(win.entries.get(inst["project"]))

    def update(self, inst: dict):
        self.inst = inst
        state = inst.get("state") or "?"
        new = state == "new"
        colour = STATE_COLOURS.get(state, "#888888") if inst.get("alive") or new else "#555555"
        self.dot.set_markup(f'<span foreground="{colour}">●</span>')
        agent = "" if inst["agent"] == registry.DEFAULT_AGENT else f' <small>{GLib.markup_escape_text(inst["agent"])}</small>'
        self.name.set_markup(f"<b>{GLib.markup_escape_text(inst['project'])}</b>{agent}")
        self.seen.set_visible(new or state in agent_status.events.DISMISSABLE)
        self.pin.show_pinned(bool(inst.get("pinned")))
        state_lbl = state.replace("_", " ") if inst.get("alive") or new else "gone"
        u = inst.get("usage") or {}
        d7 = u.get("d7") or {}
        if d7.get("turns"):
            share = u.get("share_7d_pct") or 0
            badge = f"{share:.0f}% 7d" if share >= 1 else fmt_tokens(d7.get("total_tokens"))
            state_lbl = f"{state_lbl} · {badge}"
        self.state.set_text(state_lbl)
        msg = inst.get("message") or {}
        text = (msg.get("summary") or {}).get("text") or msg.get("excerpt") or ""
        if new and not text:
            text = "New project — click to start its agent"
        self.summary.set_text(" ".join(text.split()))
        tip_parts = []
        if msg.get("excerpt"):
            tip_parts.append(msg["excerpt"].strip())
        if d7.get("turns"):
            h5 = u.get("h5") or {}
            u_lines = []
            if h5.get("turns"):
                u_lines.append(f"5h: {h5['turns']} turns · {fmt_tokens(h5['total_tokens'])} tok "
                               f"({fmt_tokens(h5['output_tokens'])} out)")
            u_lines.append(f"7d: {d7['turns']} turns · {fmt_tokens(d7['total_tokens'])} tok "
                           f"({fmt_tokens(d7['output_tokens'])} out, {u.get('share_7d_pct', 0):.1f}% of {inst['agent']})")
            tip_parts.append("\n".join(u_lines))
        self.set_tooltip_text("\n\n".join(tip_parts) if tip_parts else None)


class ProjectRow(Gtk.ListBoxRow):
    def __init__(self, win, entry: dict):
        super().__init__()
        self.set_activatable(False)
        self.win = win
        self.name = entry["name"]
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=8)
        title = Gtk.Label(xalign=0)
        title.set_markup(f"<b>{GLib.markup_escape_text(self.name)}</b>")
        self.desc = Gtk.Label(xalign=0)
        self.desc.set_ellipsize(Pango.EllipsizeMode.END)
        self.desc.get_style_context().add_class("dim-label")
        self.controls = Controls(win, self.name)
        # Its checkout: branch, worktree, subdirectory — then its tickets.
        self.gitline = Gtk.Box(spacing=4)
        self.branch = Gtk.Label(xalign=0)
        self.branch.set_ellipsize(Pango.EllipsizeMode.END)
        self.branch.get_style_context().add_class("dim-label")
        self.chips = Gtk.Box(spacing=2)
        self.drop = Gtk.Button.new_from_icon_name("user-trash-symbolic", Gtk.IconSize.MENU)
        self.drop.set_relief(Gtk.ReliefStyle.NONE)
        self.drop.set_tooltip_text("Remove this project and its worktree")
        self.drop.connect("clicked", lambda *_: tickets.remove_worktree(win, self.name))
        self.gitline.pack_start(self.branch, False, False, 0)
        self.gitline.pack_start(self.chips, False, False, 0)
        self.gitline.pack_end(self.drop, False, False, 0)
        self.gitline.set_no_show_all(True)
        self.pin = PinButton(win, self.name, "projects")
        head = Gtk.Box(spacing=4)
        head.pack_start(title, True, True, 0)
        head.pack_end(self.pin, False, False, 0)
        box.pack_start(head, False, False, 0)
        box.pack_start(self.desc, False, False, 0)
        box.pack_start(self.gitline, False, False, 0)
        box.pack_start(self.controls, False, False, 0)
        self.add(box)
        self.show_all()
        self.update(entry)

    def update(self, e: dict):
        self.entry = e
        self.pin.show_pinned(e.get("pinned", False))
        pu = self.win.project_usage.get(self.name) or {}
        u_parts = []
        for aid, lbl in (("claude", "Claude"), ("agy", "Agy")):
            d7 = (pu.get(aid) or {}).get("d7") or {}
            if d7.get("turns"):
                sh = (pu.get(aid) or {}).get("share_7d_pct") or 0
                u_parts.append(f"{lbl} {fmt_tokens(d7['total_tokens'])}" + (f" ({sh:.0f}%)" if sh >= 1 else ""))
        base_desc = e.get("desc") or e["dir"]
        u_str = "7d: " + " · ".join(u_parts) if u_parts else ""
        self.desc.set_text(f"{u_str} — {base_desc}" if u_str else base_desc)
        self.desc.set_tooltip_text(f"{e['dir']}\n{u_str}" if u_str else e["dir"])
        self.controls.update(e)
        g = e.get("git")
        keys = e.get("tickets") or []
        if g:
            bits = [f"⎇ {g['branch'] or 'detached'}"]
            if g["worktree"]:
                bits.append("worktree")
            if g["subdir"]:
                bits.append(g["subdir"])
            self.branch.set_text(" · ".join(bits))
            self.branch.set_tooltip_text(g["root"])
        if keys != getattr(self, "_keys", None):
            self._keys = keys
            for c in self.chips.get_children():
                self.chips.remove(c)
            for k in keys:
                b = Gtk.Button(label=k, relief=Gtk.ReliefStyle.NONE)
                b.get_style_context().add_class("ticket-chip")
                b.set_tooltip_text(f"Open {k}")
                b.connect("clicked", lambda *_, k=k: self.win.open_ticket(k, self.name))
                self.chips.pack_start(b, False, False, 0)
            self.chips.show_all()
        busy = any(st["up"] for st in e.get("agents", {}).values())
        self.drop.set_visible(bool(g and g["worktree"]) and not busy)
        self.branch.set_visible(bool(g))
        self.gitline.set_visible(bool(g) or bool(keys))


class Meter(Gtk.EventBox):
    """One line of the monitor: a name, a bar of how full, and the figure.
    Amber from `warn`, red from `crit`."""

    def __init__(self, name: str, on_click=None):
        super().__init__()
        box = Gtk.Box(spacing=6)
        self.name = Gtk.Label(xalign=0, width_chars=8)
        self.name.set_markup(f"<small>{GLib.markup_escape_text(name)}</small>")
        self.bar = Gtk.LevelBar(hexpand=True, valign=Gtk.Align.CENTER)
        for offset in (Gtk.LEVEL_BAR_OFFSET_LOW, Gtk.LEVEL_BAR_OFFSET_HIGH,
                       Gtk.LEVEL_BAR_OFFSET_FULL):
            self.bar.remove_offset_value(offset)
        self.bar.get_style_context().add_class("meter")
        self.value = Gtk.Label(xalign=1, width_chars=14)
        self.value.get_style_context().add_class("dim-label")
        box.pack_start(self.name, False, False, 0)
        box.pack_start(self.bar, True, True, 0)
        box.pack_start(self.value, False, False, 0)
        self.add(box)
        if on_click:
            self.connect("button-press-event", lambda *_: on_click() or True)
            self.connect("realize", lambda w: w.get_window().set_cursor(
                Gdk.Cursor.new_from_name(w.get_display(), "pointer")))

    def set(self, frac: float, text: str, tip: str | None = None,
            warn=0.75, crit=0.9):
        frac = max(0.0, min(1.0, frac))
        self.bar.set_value(frac)
        ctx = self.bar.get_style_context()
        ctx.remove_class("warn")
        ctx.remove_class("crit")
        if frac >= crit:
            ctx.add_class("crit")
        elif frac >= warn:
            ctx.add_class("warn")
        self.value.set_markup(f"<small>{GLib.markup_escape_text(text)}</small>")
        self.set_tooltip_text(tip)


def read_cpu() -> tuple[int, int]:
    """(busy, total) jiffies since boot, across every core."""
    f = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
    idle = f[3] + f[4]
    return sum(f) - idle, sum(f)


def read_mem() -> dict:
    out = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        out[k] = int(v.split()[0]) * 1024
    return out


def read_gpus() -> list[dict] | None:
    """nvidia-smi's view, or None where there is no NVIDIA GPU."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
             "temperature.gpu,power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode:
        return None
    gpus = []
    for line in out.stdout.strip().splitlines():
        name, util, used, total, temp, power = [x.strip() for x in line.split(",")]
        num = lambda s: float(s) if s.replace(".", "", 1).isdigit() else 0.0  # noqa: E731
        gpus.append({"name": name, "util": num(util), "used": num(used) * 2**20,
                     "total": num(total) * 2**20, "temp": num(temp), "power": num(power)})
    return gpus


def tmux_clients() -> dict[str, str]:
    """Each host tmux client's tty, /dev/pts/N, to the session it shows."""
    out = subprocess.run(["tmux", "list-clients", "-F", "#{client_tty}\t#{session_name}"],
                         capture_output=True, text=True).stdout
    return dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)


def gib(n: float) -> str:
    return f"{n / 2**30:.1f}G"


def until(iso: str | None) -> str:
    """'in 3h 20m' to an ISO time, for quota resets."""
    if not iso:
        return "?"
    try:
        from datetime import datetime
        secs = datetime.fromisoformat(iso).timestamp() - time.time()
    except ValueError:
        return iso
    if secs <= 0:
        return "now"
    d, rem = divmod(int(secs), 86400)
    h, m = divmod(rem // 60, 60)
    return f"in {d}d {h}h" if d else f"in {h}h {m}m" if h else f"in {m}m"


class Monitor(Gtk.Box):
    """Under the side list: the machine (CPU with the reservation book, memory,
    GPU) and what is left of each service's quota."""

    def __init__(self, open_reserve):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=3,
                         margin_start=10, margin_end=10, margin_top=6, margin_bottom=8)
        self.get_style_context().add_class("monitor")
        self.cpu = Meter("CPU")
        self.mem = Meter("memory")
        self.gpu = Meter("GPU")
        self.vram = Meter("VRAM")
        self.book = Meter("booked", on_click=open_reserve)
        system = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        for m in (self.cpu, self.book, self.mem, self.gpu, self.vram):
            system.pack_start(m, False, False, 0)
        self.quota_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        for title, body in (("System", system), ("Usage", self.quota_box)):
            ex = Gtk.Expander(expanded=True, use_markup=True,
                              label=f"<small><b>{title}</b></small>")
            ex.add(body)
            self.pack_start(ex, False, False, 0)
        self.quotas: dict[str, Meter] = {}
        self.show_all()
        for m in (self.gpu, self.vram, self.book):
            m.set_no_show_all(True)
            m.hide()

    def show_system(self, s: dict):
        top = ", ".join(f"{p['project']} {p['cores']:.1f}"
                        for p in (s.get("reserve") or {}).get("usage", {}).get("projects", [])[:6])
        self.cpu.set(s["cpu"], f"{s['cpu'] * 100:.0f}% · load {s['load']:.1f}",
                     f"{s['cores']} cores, load {s['load']:.2f}"
                     + (f"\nusing now (cores): {top}" if top else ""))
        mem = s["mem"]
        used = mem["MemTotal"] - mem["MemAvailable"]
        swap = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
        self.mem.set(used / mem["MemTotal"], f"{gib(used)} / {gib(mem['MemTotal'])}",
                     f"{gib(mem['MemAvailable'])} available · swap {gib(swap)} used")
        gpus = s.get("gpus")
        self.gpu.set_visible(bool(gpus))
        self.vram.set_visible(bool(gpus))
        if gpus:
            g = gpus[0]
            tip = "\n".join(f"{x['name']}: {x['util']:.0f}% · {gib(x['used'])} / {gib(x['total'])}"
                            f" · {x['temp']:.0f}°C · {x['power']:.0f} W" for x in gpus)
            self.gpu.set(g["util"] / 100, f"{g['util']:.0f}% · {g['temp']:.0f}°C", tip)
            self.vram.set(g["used"] / g["total"] if g["total"] else 0,
                          f"{gib(g['used'])} / {gib(g['total'])}", tip)
        r = s.get("reserve")
        self.book.set_visible(r is not None)
        if r:
            entries = r.get("entries", [])
            waiting = [e for e in entries if e["state"] == "waiting"]
            running = [e for e in entries if e["state"] == "granted"]
            text = f"{r['booked']} / {r['capacity']}" + (f" · {len(waiting)} wait" if waiting else "")
            lines = [f"{'▶' if e['state'] == 'granted' else '…'} {e['project']} "
                     f"{e['cores']}c {e.get('label') or ''}".rstrip() for e in running + waiting]
            self.book.set(r["booked"] / r["capacity"] if r["capacity"] else 0, text,
                          "\n".join(lines or ["nothing booked"]) + "\n\nclick for the CPU tab",
                          warn=0.99, crit=2)

    def show_services(self, services: dict):
        rows = []   # (id, name, used fraction, text, tip)
        for key, short in (("claude", "Claude"), ("agy", "Agy")):
            s = services.get(key)
            if not s:
                continue
            for b in (s.get("buckets") or []):
                used = float(b.get("used_pct") or 100 - float(b.get("remaining_pct") or 0))
                lbl = b.get("label", "").replace("Gemini ", "Gem ").replace("Claude/GPT ", "3P ")
                rows.append((f"{key}:{b.get('id') or lbl}", f"{short} {lbl}", used / 100,
                             f"{used:.0f}% used",
                             f"{s.get('label', key)} · {b.get('name') or lbl}: "
                             f"{100 - used:.1f}% left, resets {until(b.get('resets_at'))}"))
            cred = s.get("credits") or {}
            if cred.get("kind") == "usd" and cred.get("remaining_dollars") is not None:
                rem, lim = float(cred["remaining_dollars"]), float(cred.get("limit_dollars") or 0)
                rows.append((f"{key}:usd", f"{short} $", 1 - rem / lim if lim else 0,
                             f"${rem:.2f} left", f"grant: ${rem:.2f} of ${lim:.0f} left, "
                             f"resets {until(cred.get('resets_at'))}"))
            elif cred.get("kind") == "credits" and cred.get("remaining_credits") is not None:
                rem = int(cred["remaining_credits"])
                rows.append((f"{key}:credits", f"{short} cr", 1.0 if rem == 0 else 0.0,
                             f"{rem} credits", f"Antigravity credits: {rem}"))
            u = s.get("usage") or {}
            for win, label in (("h5", "5h"), ("d7", "7d")):
                w = u.get(win) or {}
                if w.get("turns") and rows and rows[-1][0].startswith(key):
                    rid, name, frac, text, tip = rows[-1]
                    rows[-1] = (rid, name, frac, text, f"{tip}\n{s.get('label', key)} {label}: "
                                f"{w['turns']} turns · {fmt_tokens(w['total_tokens'])} tokens "
                                f"({fmt_tokens(w['output_tokens'])} out)")
        ids = [r[0] for r in rows]
        for rid in set(self.quotas) - set(ids):
            self.quota_box.remove(self.quotas.pop(rid))
        for i, (rid, name, frac, text, tip) in enumerate(rows):
            m = self.quotas.get(rid)
            if m is None:
                m = self.quotas[rid] = Meter(name)
                self.quota_box.pack_start(m, False, False, 0)
                m.show_all()
            self.quota_box.reorder_child(m, i)
            m.set(frac, text, tip)


class Window(Gtk.ApplicationWindow):
    def __init__(self, app, base: str | None):
        super().__init__(application=app, title="Radio Room")
        self.base = base      # the voice server, when the "voice" feature is on
        self.set_default_size(1500, 900)
        self.tabs: dict[tuple, Gtk.Widget] = {}
        self.rows: dict[tuple, AgentRow] = {}
        self.project_usage: dict[str, dict] = {}
        self.entries: dict[str, dict] = {}     # registry status, by project
        self.sessions: dict[str, tuple] = {}   # host tmux session -> (project, agent)
        self.reserve_url: str | None = None
        self.colours: dict[str, str] = {}     # project -> its tabs' dot
        # What tickets.py reaches through the window.
        self.registry, self.jira, self.agent_status = registry, registry.jira, agent_status
        self.order: dict[str, int] = {}        # project -> its place in the registry
        self.repo_counts: dict[str, int] = {}  # repository -> how many projects in it

        header = Gtk.HeaderBar(show_close_button=True, title="Radio Room")
        new = Gtk.Box()
        new.get_style_context().add_class("linked")
        buttons = [("utilities-terminal-symbolic", "Terminal", "New terminal (Ctrl+T)",
                    lambda *_: self.open_shell())]
        if radio_config.feature("launcher"):
            buttons.append(("tab-new-symbolic", "Launcher", "New launcher tab (Ctrl+Shift+T)",
                            lambda *_: self.open_launcher()))
        for icon, label, tip, act in buttons:
            b = Gtk.Button(label=label, always_show_image=True)
            b.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
            b.set_tooltip_text(tip)
            b.connect("clicked", act)
            new.pack_start(b, False, False, 0)
        header.pack_start(new)
        self.status = Gtk.Label()
        self.status.get_style_context().add_class("dim-label")
        header.pack_end(self.status)
        self.set_titlebar(header)

        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        # A new project's card has no session yet: clicking it starts one.
        self.list.connect("row-activated", lambda _l, row: self.open_agent(
            row.inst["project"], row.inst["agent"], start=row.inst["state"] == "new"))
        agents = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        agents.add(self.list)

        self.project_rows: dict[str, ProjectRow] = {}
        self.projects = Gtk.ListBox()
        self.projects.set_selection_mode(Gtk.SelectionMode.NONE)
        self.search = Gtk.SearchEntry(placeholder_text="Filter projects", margin=6)
        self.search.connect("search-changed", lambda *_: self.projects.invalidate_filter())
        self.projects.set_filter_func(
            lambda row: self.search.get_text().lower() in row.name.lower())
        # Grouped by repository, so a monorepo's worktrees sit together;
        # projects outside git come last.
        self.projects.set_sort_func(lambda a, b: (self.group_key(a) > self.group_key(b))
                                    - (self.group_key(a) < self.group_key(b)))
        self.projects.set_header_func(self.project_header)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.add(self.projects)
        projects = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        projects.pack_start(self.search, False, False, 0)
        projects.pack_start(scroll, True, True, 0)

        stack = Gtk.Stack()
        stack.add_titled(agents, "agents", "Agents")
        stack.add_titled(projects, "projects", "Projects")
        self.tickets = None
        if registry.jira.enabled():
            self.tickets = tickets.TicketsView(self)
            stack.add_titled(self.tickets, "tickets", "Tickets")
        self.cpu = cpu_view.CpuView(self)
        stack.add_titled(self.cpu, "cpu", "CPU")
        self.stack = stack
        switcher = Gtk.StackSwitcher(stack=stack, halign=Gtk.Align.CENTER, margin=6)
        self.monitor = Monitor(self.open_reserve)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side.set_size_request(340, -1)
        side.pack_start(switcher, False, False, 0)
        side.pack_start(stack, True, True, 0)
        side.pack_end(self.monitor, False, False, 0)

        self.book = Gtk.Notebook(scrollable=True)
        self.book.popup_enable()

        paned = Gtk.Paned()
        paned.pack1(side, False, False)
        paned.pack2(self.book, True, False)
        self.add(paned)

        if self.base:
            self.add_web_tab()
        self.quitting = False
        self.restore()
        self.book.connect("page-reordered", lambda *_: self.save())
        self.book.connect_after("switch-page", lambda *_: self.save())
        self.connect("delete-event", self._quit)
        # Also every few seconds, so a window that is killed rather than closed
        # still comes back with its tabs (and its web tabs at their pages).
        GLib.timeout_add_seconds(10, lambda: self.save() or True)
        GLib.timeout_add(int(TITLE_POLL * 1000), lambda: self.follow_titles() or True)

        accel = Gtk.AccelGroup()
        ctrl, shift = Gdk.ModifierType.CONTROL_MASK, Gdk.ModifierType.SHIFT_MASK
        accel.connect(Gdk.KEY_t, ctrl, 0, lambda *_: self.open_shell() or True)
        if radio_config.feature("launcher"):
            accel.connect(Gdk.KEY_t, ctrl | shift, 0, lambda *_: self.open_launcher() or True)
        accel.connect(Gdk.KEY_w, ctrl | shift, 0, lambda *_: self.close_current() or True)
        self.add_accel_group(accel)

        threading.Thread(target=self.poll, daemon=True).start()
        threading.Thread(target=self.poll_projects, daemon=True).start()
        threading.Thread(target=self.poll_system, daemon=True).start()

    # -- tabs ---------------------------------------------------------------

    def tab_label(self, text: str, page: Gtk.Widget, closable=True) -> Gtk.Widget:
        # A terminal, a web page, or (not closable) Radio Room's own page;
        # the dot is the project's colour, which its tabs share.
        icon = ("utilities-terminal-symbolic" if isinstance(page, Terminal)
                else "web-browser-symbolic" if closable else APP_ID)
        box = Gtk.Box(spacing=4)
        page.tab_dot = Gtk.Label()
        page.tab_dot.set_no_show_all(True)
        page.tab_text = Gtk.Label(label=text, max_width_chars=28,
                                  width_chars=min(len(text) + 1, 28))
        page.tab_text.set_ellipsize(Pango.EllipsizeMode.END)
        box.pack_start(page.tab_dot, False, False, 0)
        box.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU), False, False, 0)
        box.pack_start(page.tab_text, True, True, 0)
        if closable:
            x = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
            x.set_relief(Gtk.ReliefStyle.NONE)
            x.connect("clicked", lambda *_: self.close_tab(page))
            box.pack_start(x, False, False, 0)
        box.show_all()
        return box

    def pages(self) -> list[Gtk.Widget]:
        return [self.book.get_nth_page(n) for n in range(self.book.get_n_pages())]

    def cluster_end(self, group: str | None, besides: Gtk.Widget | None = None) -> int:
        """Where a tab of `group` goes: after that project's last tab (not
        counting `besides`, the tab being moved); -1 when it has none."""
        others = [p for p in self.pages() if p is not besides]
        last = max((i for i, p in enumerate(others)
                    if group is not None and getattr(p, "group", None) == group), default=None)
        return -1 if last is None else last + 1

    def add_page(self, page: Gtk.Widget, title: str, closable=True,
                 group: str | None = None, after: Gtk.Widget | None = None):
        page.show_all()
        pos = self.cluster_end(group)
        if pos < 0 and after is not None and self.book.page_num(after) >= 0:
            pos = self.book.page_num(after) + 1
        n = self.book.insert_page(page, self.tab_label(title, page, closable), pos)
        self.set_group(page, group)
        self.book.set_tab_reorderable(page, True)
        if isinstance(page, (Terminal, WebPage)):
            page.on_link = lambda url, out, src=page: self.open_link(src, url, out)
        if isinstance(page, WebPage):
            page.on_title = self.web_title
            page.on_new = lambda src=page: self.open_page(src)
        self.book.set_current_page(n)
        if isinstance(page, Terminal):
            page.vte.grab_focus()
        elif isinstance(page, WebPage):
            page.view.grab_focus()

    def set_group(self, page: Gtk.Widget, group: str | None):
        """Put a tab in a project's cluster: its colour, and beside the rest."""
        moved = group != getattr(page, "group", None)
        page.group = group
        page.tab_dot.set_visible(group is not None)
        if group is not None:
            page.tab_dot.set_markup(f'<span foreground="{self.group_colour(group)}">●</span>')
            page.tab_dot.set_tooltip_text(group)
            pos = self.cluster_end(group, besides=page)
            if moved and pos >= 0:
                self.book.reorder_child(page, pos)

    def group_colour(self, group: str) -> str:
        """The project's colour while it has tabs: the first not already
        taken by another open project, so neighbours never look alike."""
        taken = {self.colours.get(p.group) for p in self.pages()
                 if getattr(p, "group", None) not in (None, group)}
        if self.colours.get(group) in taken | {None}:
            free = [c for c in GROUP_COLOURS if c not in taken] or GROUP_COLOURS
            self.colours[group] = free[0]
        return self.colours[group]

    def set_title(self, page: Gtk.Widget, text: str):
        if page.tab_text.get_text() != text:
            page.tab_text.set_text(text)
            page.tab_text.set_width_chars(min(len(text) + 1, 28))
            page.tab_text.get_parent().set_tooltip_text(text)

    def close_tab(self, page: Gtk.Widget):
        for key, t in list(self.tabs.items()):
            if t is page:
                del self.tabs[key]
        n = self.book.page_num(page)
        if n >= 0:
            self.book.remove_page(n)   # drops the PTY; tmux sees a detach
        self.save()

    def close_current(self):
        page = self.book.get_nth_page(self.book.get_current_page())
        if isinstance(page, Terminal) or getattr(page, "closable", False):
            self.close_tab(page)

    def current_cwd(self) -> str | None:
        page = self.book.get_nth_page(self.book.get_current_page())
        return page.cwd() if isinstance(page, Terminal) and page.memo and \
            page.memo["kind"] == "shell" else None

    def open_shell(self, cwd: str | None = None):
        """A plain terminal: a login shell, no launcher, in the current
        terminal's directory as a new tab in any terminal would be."""
        cwd = cwd or self.current_cwd()
        term = Terminal(["bash", "-li"], self.close_tab, cwd=cwd)
        term.memo = {"kind": "shell"}
        self.add_page(term, "terminal")
        self.save()

    def open_launcher(self):
        # The same way in as SSH and the browser terminal: a login shell,
        # whose ~/.bashrc runs the launcher when SSH_CLIENT_WEB is set.
        term = Terminal(["bash", "-li"], self.close_tab, {"SSH_CLIENT_WEB": "1"})
        term.memo = {"kind": "launcher"}
        self.add_page(term, "launcher")
        self.save()

    def open_session(self, session: str):
        """A tab attached to a host tmux session by name — how a launcher tab
        that had picked a project comes back after a restart."""
        term = Terminal(["tmux", "attach-session", "-t", f"={session}"], self.close_tab)
        term.memo = {"kind": "session", "session": session}
        owner = self.sessions.get(session)
        if owner:
            self.tabs.setdefault(owner, term)
        self.add_page(term, self.agent_title(*owner) if owner else session,
                      group=owner[0] if owner else None)
        self.save()

    @staticmethod
    def agent_title(project: str, agent: str) -> str:
        return project if agent == registry.DEFAULT_AGENT else f"{project} · {agent}"

    def open_agent(self, project: str, agent: str, start=False):
        """A tab on the agent's session; start=True creates the session first
        if it isn't running, as the launcher would."""
        key = (project, agent)
        if key in self.tabs:
            page = self.tabs[key]
            self.book.set_current_page(self.book.page_num(page))
            page.vte.grab_focus()
            return
        term = Terminal([sys.executable, str(START if start else ATTACH), *key], self.close_tab)
        term.memo = {"kind": "agent", "project": project, "agent": agent}
        self.tabs[key] = term
        self.add_page(term, self.agent_title(*key), group=project)
        self.save()

    def follow_titles(self):
        """Name a free terminal (a launcher, or a plain shell that has run
        tmux) after the session it is attached to, and cluster it with that
        project's tabs: a host session is read off tmux by the tab's tty, a
        container's off the `docker exec` the launcher runs. Otherwise a busy
        terminal is named by its title (or as the launcher, at its menu), and
        one at its prompt by its directory."""
        clients = tmux_clients()
        for page in self.pages():
            if not isinstance(page, Terminal) or not page.memo or \
                    page.memo["kind"] not in ("launcher", "shell"):
                continue
            busy = not page.at_prompt()
            owner = (self.sessions.get(clients.get(page.tty()))
                     or self.sessions.get(page.docker_session())) if busy else None
            if owner != getattr(page, "owner", None):
                old = getattr(page, "owner", None)
                if old and self.tabs.get(old) is page:
                    del self.tabs[old]
                if owner:
                    self.tabs.setdefault(owner, page)
                page.owner = owner
                self.set_group(page, owner[0] if owner else None)
            if owner:
                self.set_title(page, self.agent_title(*owner))
            elif busy:
                self.set_title(page, clients.get(page.tty()) or page.title() or
                               ("launcher" if page.memo["kind"] == "launcher" else page.where()))
            else:
                self.set_title(page, page.where() or "terminal")

    def trusted_hosts(self) -> set[str]:
        """Hosts whose self-signed certificates a tab accepts: this machine
        and the registered services'."""
        return {"localhost", "127.0.0.1"} | {e["host"] for e in self.entries.values() if e.get("host")}

    def add_web_tab(self):
        page = WebPage(self.base + "/", self.trusted_hosts(), closable=False)
        page.fixed_title = "Radio Room"
        self.add_page(page, "Radio Room", closable=False)

    def open_web(self, name: str, url: str):
        """A browser tab on a project's web instance, or the one already open."""
        key = ("web", name)
        if key in self.tabs:
            self.book.set_current_page(self.book.page_num(self.tabs[key]))
            return
        page = self.tabs[key] = WebPage(url, self.trusted_hosts() | {host_of(url)})
        page.memo = {"kind": "web", "name": name, "url": url}
        page.fixed_title = name
        self.add_page(page, name, group=name)
        self.save()

    def open_link(self, src: Gtk.Widget | None, url: str, out: bool):
        """A link from a terminal or a page: into a tab beside its source, in
        the source's project cluster — or out to the real browser."""
        if out:
            Gtk.show_uri_on_window(self, url, Gdk.CURRENT_TIME)
            return
        self.open_page(src, url)

    def open_page(self, src: Gtk.Widget | None, url: str | None = None,
                  group: str | None = None) -> WebPage:
        """A new web tab; with no url, a page's own new window, which WebKit
        then loads into."""
        related = src.view if url is None and isinstance(src, WebPage) else None
        page = WebPage(url, self.trusted_hosts(), related=related)
        page.memo = {"kind": "link", "url": url}
        group = group or getattr(src, "group", None)
        self.add_page(page, host_of(url) if url else "new tab", group=group, after=src)
        self.save()
        return page

    def web_title(self, page: WebPage):
        if not getattr(page, "fixed_title", None):
            self.set_title(page, page.title())

    def open_reserve(self):
        """The booked meter: to the CPU tab, which steers the queue."""
        self.stack.set_visible_child_name("cpu")

    # -- remembering tabs ---------------------------------------------------

    def memo(self, page, clients: dict) -> dict | None:
        """What reopens this tab: a web tab at the page it is on now, a plain
        terminal in its directory, and a launcher tab as whatever host session
        it has since attached to (read off tmux by its tty, as terminal/
        names its tabs) or as a launcher."""
        m = getattr(page, "memo", None)
        if m is None:
            return None
        if m["kind"] in ("web", "link"):
            m = {**m, "url": page.view.get_uri() or m["url"]}
            if m["kind"] == "link":
                m["group"] = page.group
            return m if m["url"] else None
        if m["kind"] == "shell":
            return {**m, "cwd": page.cwd()}
        if m["kind"] == "launcher" and clients.get(page.tty()):
            return {"kind": "session", "session": clients[page.tty()]}
        return m

    def save(self):
        if self.quitting is not False:   # restoring, or the window is going
            return
        clients = tmux_clients()
        tabs, current = [], 0
        for n in range(self.book.get_n_pages()):
            m = self.memo(self.book.get_nth_page(n), clients)
            if m is not None:
                if n == self.book.get_current_page():
                    current = len(tabs)
                tabs.append(m)
        try:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"tabs": tabs, "current": current}, indent=1))
            tmp.replace(STATE)
        except OSError as exc:
            self.status.set_text(f"can't save tabs: {exc}"[:80])

    def restore(self):
        """Reopen last time's tabs. An agent whose session has stopped since
        is left out rather than opened onto an error; with nothing to
        restore, there is one plain terminal, as on a first start."""
        try:
            state = json.loads(STATE.read_text())
        except (OSError, ValueError):
            state = {}
        self.quitting = None
        self.load_sessions(registry.status(registry.load()))
        opened = 0
        for m in state.get("tabs", []):
            kind = m.get("kind")
            if kind == "agent":
                try:
                    resolve(m["project"], m["agent"])
                except AttachError:
                    continue
                self.open_agent(m["project"], m["agent"])
            elif kind == "session":
                if subprocess.run(["tmux", "has-session", "-t", f"={m['session']}"],
                                  capture_output=True).returncode:
                    continue
                self.open_session(m["session"])
            elif kind == "web":
                self.open_web(m["name"], m["url"])
            elif kind == "link":
                self.open_page(None, m["url"], group=m.get("group"))
            elif kind == "launcher":
                if radio_config.feature("launcher"):
                    self.open_launcher()
                else:
                    self.open_shell(str(Path.home()))
            elif kind == "shell":
                cwd = m.get("cwd")
                self.open_shell(cwd if cwd and os.path.isdir(cwd) else str(Path.home()))
            else:
                continue
            opened += 1
        if not opened:
            self.open_shell(str(Path.home()))
        else:
            # the Radio Room tab, when there is one, is page 0
            self.book.set_current_page((1 if self.base else 0)
                                       + min(state.get("current", 0), opened - 1))
        self.quitting = False

    def _quit(self, *_):
        self.save()          # while every tab's client is still attached
        self.quitting = True
        return False

    # -- projects -----------------------------------------------------------

    def poll_projects(self):
        while True:
            try:
                entries = registry.status(registry.load())
                GLib.idle_add(self.show_projects, entries)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self.status.set_text, f"registry: {exc}"[:80])
            time.sleep(PROJECT_POLL)

    def refresh_projects(self):
        threading.Thread(target=lambda: GLib.idle_add(
            self.show_projects, registry.status(registry.load())), daemon=True).start()

    def load_sessions(self, entries):
        self.entries = {e["name"]: e for e in entries}
        # host sessions by name, container ones as container:session
        self.sessions = {st["session"]: (e["name"], a) for e in entries
                         for a, st in e["agents"].items() if st.get("session")}

    def shared_repo(self, name: str) -> dict | None:
        """The project's git facts when its repository holds other projects
        too (a monorepo's worktrees), else None: a standalone repository is
        grouped with the rest of them rather than under a heading of its own."""
        g = (self.entries.get(name) or {}).get("git")
        return g if g and self.repo_counts.get(g["repo"], 0) > 1 else None

    def group_key(self, row) -> tuple:
        # Pinned projects come first, out of their repository's group.
        if (self.entries.get(row.name) or {}).get("pinned"):
            return (-1, "", "", self.order.get(row.name, 0))
        g = self.shared_repo(row.name)
        in_git = bool((self.entries.get(row.name) or {}).get("git"))
        if g:
            return (0, g["name"].lower(), g["repo"], self.order.get(row.name, 0))
        return (1 if in_git else 2, "", "", self.order.get(row.name, 0))

    def project_header(self, row, before):
        key = self.group_key(row)[:3]
        if before is not None and self.group_key(before)[:3] == key:
            row.set_header(None)
            return
        g = self.shared_repo(row.name) if key[0] >= 0 else None
        title = (g["name"] if g else "Pinned" if key[0] == -1
                 else "Projects" if key[0] == 1 else "Not in git")
        box = Gtk.Box(spacing=4)
        box.get_style_context().add_class("repo-header")
        lbl = Gtk.Label(xalign=0)
        lbl.set_markup(f"<small><b>{GLib.markup_escape_text(title)}</b></small>")
        lbl.set_tooltip_text(g["repo"] if g else "")
        box.pack_start(lbl, True, True, 0)
        if g:
            add = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.MENU)
            add.set_relief(Gtk.ReliefStyle.NONE)
            add.set_tooltip_text(f"New worktree of {g['name']}")
            add.connect("clicked", lambda *_, r=g["repo"]: tickets.new_worktree(self, repo=r))
            box.pack_end(add, False, False, 0)
        box.show_all()
        row.set_header(box)

    def open_ticket(self, key: str, group: str | None = None):
        base = registry.jira.settings()["url"].rstrip("/")
        if base:
            self.open_page(None, f"{base}/browse/{key}", group=group)

    def show_projects(self, entries):
        self.load_sessions(entries)
        self.order = {e["name"]: i for i, e in enumerate(entries)}
        self.repo_counts: dict[str, int] = {}
        for e in entries:
            if e.get("git"):
                self.repo_counts[e["git"]["repo"]] = self.repo_counts.get(e["git"]["repo"], 0) + 1
        names = []
        for i, e in enumerate(entries):
            names.append(e["name"])
            row = self.project_rows.get(e["name"])
            if row is None:
                row = self.project_rows[e["name"]] = ProjectRow(self, e)
                self.projects.insert(row, i)
            else:
                row.update(e)
        for name in set(self.project_rows) - set(names):
            self.projects.remove(self.project_rows.pop(name))
        for (project, _), row in self.rows.items():
            row.controls.update(self.entries.get(project))
        self.projects.invalidate_sort()
        self.projects.invalidate_headers()
        if self.tickets is not None:
            self.tickets.relink()
        return False

    def confirm(self, text: str, detail: str) -> bool:
        d = Gtk.MessageDialog(transient_for=self, modal=True,
                              message_type=Gtk.MessageType.WARNING,
                              buttons=Gtk.ButtonsType.OK_CANCEL, text=text)
        d.format_secondary_text(detail)
        ok = d.run() == Gtk.ResponseType.OK
        d.destroy()
        return ok

    def stop_agent(self, name: str, agent: str):
        p = registry.find(registry.load(), name)
        if p is None or not self.confirm(
                f"Stop {name}'s {agent} session?",
                "This kills the tmux session and the agent in it, mid-turn or not."):
            return
        ok, err = registry.stop_agent(p, agent)
        self.status.set_text(f"stopped {name} ({agent})" if ok else f"{name}: {err}"[:80])
        self.refresh_projects()

    def web(self, name: str, up: bool):
        """Bring a web instance up or down off the main thread — start scripts
        can take a while — the way the hub's buttons do."""
        p = registry.find(registry.load(), name)
        if p is None:
            return
        if not up and not self.confirm(f"Stop {name}'s web instance?", ""):
            return
        self.status.set_text(f"{'starting' if up else 'stopping'} {name} web…")

        def run():
            ok, msg = (registry.start_web if up else registry.stop_web)(p)
            if ok and up:
                GLib.idle_add(self.open_started_web, name)
            GLib.idle_add(self.status.set_text,
                          f"{name} web: {'ok' if ok else 'failed'}{' — ' + msg if msg else ''}"[:80])
            self.refresh_projects()

        threading.Thread(target=run, daemon=True).start()

    def open_started_web(self, name: str):
        e = next((x for x in registry.status(registry.load()) if x["name"] == name), None)
        if e and e["port"]:
            self.open_web(name, web_url(e))
        return False

    # -- the machine --------------------------------------------------------

    def poll_system(self):
        """CPU, memory and GPU every couple of seconds, and the reservation
        book when its server is registered and up."""
        out = subprocess.run([PROJ, "url", RESERVE_PROJECT], capture_output=True, text=True)
        self.reserve_url = out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None
        prev = read_cpu()
        while True:
            time.sleep(MONITOR_POLL)
            try:
                cur = read_cpu()
                busy, total = cur[0] - prev[0], cur[1] - prev[1]
                prev = cur
                s = {"cpu": busy / total if total else 0.0,
                     "load": float(Path("/proc/loadavg").read_text().split()[0]),
                     "cores": os.cpu_count(), "mem": read_mem(), "gpus": read_gpus()}
                if self.reserve_url:
                    try:
                        with urllib.request.urlopen(self.reserve_url.rstrip("/") + "/api/state",
                                                    timeout=2) as r:
                            s["reserve"] = json.load(r)
                    except (OSError, ValueError):
                        pass
                GLib.idle_add(self.monitor.show_system, s)
            except Exception as exc:  # noqa: BLE001 — the monitor never takes the window down
                GLib.idle_add(self.status.set_text, f"monitor: {exc}"[:80])

    # -- agents -------------------------------------------------------------

    def fetch(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=5, context=INSECURE) as r:
            return json.load(r)

    def poll(self):
        """Agent status, read straight from the store (voice/status.py) — the
        voice server need not be running. With it, also follow what the voice
        operator moves to."""
        desk = None   # what the voice side has already asked for, before we looked
        while True:
            try:
                data = agent_status.instances_payload()
                GLib.idle_add(self.show_instances, data.get("instances", []), None,
                              data.get("services"), data.get("project_usage"))
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self.show_instances, None, str(exc), None, None)
            if self.base:
                desk = self.follow_desk(desk)
            time.sleep(POLL)

    def dismiss(self, project: str, agent: str):
        """Mark an agent's finished turn seen, then redraw from the store."""
        def work():
            agent_status.dismiss_instance(project, agent)
            return agent_status.instances_payload()
        tickets.run_bg(self, "dismiss", work, lambda d: self.show_instances(
            d.get("instances", []), None, d.get("services"), d.get("project_usage")))

    def pin(self, project: str, view: str, pinned: bool):
        """Pin or unpin a project in the Agents or Projects view, then redraw
        that view."""
        def work():
            agent_status.set_pin(project, view, pinned)
            return agent_status.instances_payload() if view == "agents" else None
        def done(d):
            if view == "agents":
                self.show_instances(d.get("instances", []), None,
                                    d.get("services"), d.get("project_usage"))
            else:
                self.refresh_projects()
        tickets.run_bg(self, "pin", work, done)

    def follow_desk(self, desk: int | None) -> int | None:
        try:
            feed = self.fetch(f"/api/desk?after={desk or 0}")
            if desk is not None and feed["seq"] < desk:
                # voice/ restarted and its count began again: everything
                # it has now is new to us.
                feed = self.fetch("/api/desk?after=0")
        except Exception:  # noqa: BLE001 — the voice server is down; try again
            return desk
        if desk is not None:
            for event in feed["events"]:
                GLib.idle_add(self.follow, event)
        return feed["seq"]

    def follow(self, event: dict):
        """Put in front of the user what the voice operator just moved to:
        a project's agent, a web app it brought up, or the operator itself."""
        kind = event.get("kind")
        if kind == "agent":
            self.open_agent(event["project"], event.get("agent") or registry.DEFAULT_AGENT)
        elif kind == "web":
            self.open_started_web(event["project"])
        elif kind == "operator":
            self.book.set_current_page(0)
        return False

    def show_instances(self, instances, error, services=None, project_usage=None):
        if error is not None:
            self.status.set_text(f"agent status: {error[:60]}")
            return False
        self.status.set_text(time.strftime("updated %H:%M:%S"))
        if services:
            self.monitor.show_services(services)
        if project_usage and project_usage != self.project_usage:
            self.project_usage = project_usage
            for row in self.project_rows.values():
                row.update(row.entry)
        seen = []
        for i, inst in enumerate(instances):
            key = (inst["project"], inst["agent"])
            seen.append(key)
            row = self.rows.get(key)
            if row is None:
                row = self.rows[key] = AgentRow(self, inst)
                self.list.insert(row, i)
            else:
                row.update(inst)
                if row.get_index() != i:   # keep voice/'s most-recent-first order
                    self.list.remove(row)
                    self.list.insert(row, i)
        for key in set(self.rows) - set(seen):
            self.list.remove(self.rows.pop(key))
        return False


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        # The icon is found here without being installed; installed (see the
        # README) the shell finds it too, for the launcher and the dock.
        Gtk.IconTheme.get_default().append_search_path(str(ICONS))
        Gtk.Window.set_default_icon_name(APP_ID)
        self.base = voice_url() if radio_config.feature("voice") else None
        # Watch the agents from here unless something else already does (the
        # voice server); take over if it stops.
        agent_status.start()

    def do_activate(self):
        win = self.get_active_window()
        if win is None:
            css = Gtk.CssProvider()
            css.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            win = Window(self, self.base)
            win.show_all()
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv[:1]))

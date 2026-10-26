"""Jira tickets and worktrees in the desktop window.

The Tickets view is the config's JQL (your open tickets, by default), each
with the projects it is linked to. Clicking one offers what can be done with
it: open it, start work on it in a new worktree, send it to a linked
project's agent, link or unlink it, move it, comment, assign it to yourself.
New tickets come from the button above the list.

The worktree dialog is shared with the Projects view, where each repository
heading has one: a new worktree of that repository, registered as a project
(optionally for a ticket, and a subdirectory of the checkout), its agent
started.

Everything that talks to Jira or git runs off the main loop; failures land
in the window's status line.
"""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

CATEGORY_COLOURS = {"new": "#77767b", "indeterminate": "#3584e4", "done": "#2ec27e"}
JIRA_POLL = 120   # seconds between refreshes of the ticket list


def run_bg(win, what: str, work, done=None):
    """work() off the main loop, then done(result) on it; an error goes to
    the status line, prefixed with `what`."""
    def go():
        try:
            res = work()
        except Exception as exc:  # noqa: BLE001 — shown, never raised into GTK
            GLib.idle_add(win.status.set_text, f"{what}: {exc}"[:120])
            return
        if done is not None:
            GLib.idle_add(lambda: done(res) and False)
    threading.Thread(target=go, daemon=True).start()


def form(parent, title: str, fields: list[tuple], ok: str = "OK") -> dict | None:
    """A small modal form. Each field is (key, label, kind, default[, options])
    with kind "entry", "text" (several lines), "combo" (options: [(id, label)])
    or "check". Returns {key: value}, or None if cancelled."""
    d = Gtk.Dialog(title=title, transient_for=parent, modal=True)
    d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, ok, Gtk.ResponseType.OK)
    d.set_default_response(Gtk.ResponseType.OK)
    d.set_default_size(460, -1)
    grid = Gtk.Grid(row_spacing=8, column_spacing=12, margin=12)
    widgets = {}
    for row, f in enumerate(fields):
        key, label, kind, default = f[:4]
        if kind == "check":
            w = Gtk.CheckButton(label=label, active=bool(default))
            grid.attach(w, 1, row, 1, 1)
        else:
            grid.attach(Gtk.Label(label=label, xalign=1), 0, row, 1, 1)
            if kind == "text":
                view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
                view.get_buffer().set_text(default or "")
                w = Gtk.ScrolledWindow(min_content_height=120, hexpand=True)
                w.add(view)
                w.view = view
            elif kind == "combo":
                w = Gtk.ComboBoxText(hexpand=True)
                for oid, olabel in f[4]:
                    w.append(oid, olabel)
                w.set_active_id(default if default is not None else f[4][0][0] if f[4] else None)
            else:
                w = Gtk.Entry(text=default or "", hexpand=True, activates_default=True)
            grid.attach(w, 1, row, 1, 1)
        widgets[key] = (kind, w)
    d.get_content_area().add(grid)
    d.show_all()
    resp = d.run()
    out = None
    if resp == Gtk.ResponseType.OK:
        out = {}
        for key, (kind, w) in widgets.items():
            if kind == "text":
                buf = w.view.get_buffer()
                out[key] = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
            elif kind == "combo":
                out[key] = w.get_active_id()
            elif kind == "check":
                out[key] = w.get_active()
            else:
                out[key] = w.get_text().strip()
    d.destroy()
    return out


# ── Worktrees ────────────────────────────────────────────────────────────────

def repos(entries: dict) -> list[tuple[str, str, str]]:
    """(repo id, display name, a project directory in it) for every git
    repository a registered project lives in, main checkouts first."""
    seen: dict[str, tuple[str, str, str]] = {}
    for e in sorted(entries.values(), key=lambda e: bool((e.get("git") or {}).get("worktree"))):
        g = e.get("git")
        if g and g["repo"] not in seen:
            seen[g["repo"]] = (g["repo"], g["name"], e["dir"])
    return sorted(seen.values(), key=lambda r: r[1].lower())


def new_worktree(win, repo: str | None = None, ticket: dict | None = None):
    """Ask for a new worktree project, make it, and open its agent."""
    registry, jira = win.registry, win.jira
    choices = repos(win.entries)
    if not choices:
        win.status.set_text("no git repository among the registered projects")
        return
    default_repo = next((r[0] for r in choices if r[0] == repo), choices[0][0])
    key = ticket["key"] if ticket else ""
    name = jira.branch_for(key, ticket["summary"]).lower()[:40].rstrip("-") if ticket else ""
    branch = jira.branch_for(key, ticket["summary"]) if ticket else ""
    v = form(win, f"Start work on {key}" if ticket else "New worktree", [
        ("repo", "Repository", "combo", default_repo, [(r[0], r[1]) for r in choices]),
        ("name", "Project name", "entry", name),
        ("branch", "Branch", "entry", branch),
        ("base", "From (new branch)", "entry", ""),
        ("subdir", "Subdirectory", "entry", ""),
        ("ticket", "Ticket", "entry", key),
        ("start", "Start the agent", "check", True),
    ], ok="Create")
    if not v or not v["name"]:
        return
    src = next(r[2] for r in choices if r[0] == v["repo"])
    win.status.set_text(f"creating worktree {v['name']}…")

    def work():
        return registry.add_worktree_project(
            v["name"], src, branch=v["branch"] or None, base=v["base"] or None,
            subdir=v["subdir"], ticket=(v["ticket"] or "").upper() or None)

    def done(p):
        win.status.set_text(f"created {p['name']} at {p['dir']}")
        win.refresh_projects()
        if v["start"]:
            win.open_agent(p["name"], registry.DEFAULT_AGENT, start=True)
    run_bg(win, "worktree", work, done)


def remove_worktree(win, name: str):
    e = win.entries.get(name) or {}
    g = e.get("git") or {}
    if not g.get("worktree"):
        return
    if not win.confirm(f"Remove {name} and its worktree?",
                       f"{g['root']} is deleted (the branch {g['branch']} is kept). "
                       "Refused if it has uncommitted changes."):
        return
    run_bg(win, "remove", lambda: win.registry.remove_project(name, worktree=True),
           lambda lines: (win.status.set_text("; ".join(lines)[:120]), win.refresh_projects()))


# ── Tickets ──────────────────────────────────────────────────────────────────

class TicketRow(Gtk.ListBoxRow):
    def __init__(self, issue: dict):
        super().__init__()
        self.issue = issue
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin=8)
        top = Gtk.Box(spacing=6)
        self.key = Gtk.Label(xalign=0, hexpand=True)
        self.state = Gtk.Label(xalign=1)
        top.pack_start(self.key, True, True, 0)
        top.pack_end(self.state, False, False, 0)
        self.summary = Gtk.Label(xalign=0)
        self.summary.set_ellipsize(Pango.EllipsizeMode.END)
        self.links = Gtk.Label(xalign=0)
        self.links.get_style_context().add_class("dim-label")
        self.links.set_no_show_all(True)
        box.pack_start(top, False, False, 0)
        box.pack_start(self.summary, False, False, 0)
        box.pack_start(self.links, False, False, 0)
        self.add(box)
        self.show_all()

    def update(self, issue: dict, linked: list[str]):
        self.issue = issue
        esc = GLib.markup_escape_text
        self.key.set_markup(f"<b>{esc(issue['key'])}</b> <small>{esc(issue['type'])}</small>")
        colour = CATEGORY_COLOURS.get(issue["category"], "#77767b")
        self.state.set_markup(f'<span foreground="{colour}">{esc(issue["status"])}</span>')
        self.summary.set_text(issue["summary"])
        self.summary.set_tooltip_text(issue["summary"])
        self.links.set_text("→ " + ", ".join(linked) if linked else "")
        self.links.set_visible(bool(linked))


class TicketsView(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.issues: list[dict] = []
        self.rows: dict[str, TicketRow] = {}
        bar = Gtk.Box(spacing=4, margin=6)
        self.count = Gtk.Label(xalign=0, hexpand=True)
        self.count.get_style_context().add_class("dim-label")
        for icon, tip, act in (("list-add-symbolic", "New ticket", self.new_ticket),
                               ("view-refresh-symbolic", "Refresh", self.refresh)):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda *_, a=act: a())
            bar.pack_end(b, False, False, 0)
        bar.pack_start(self.count, True, True, 0)
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list.connect("row-activated", lambda _l, row: self.actions(row))
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.add(self.list)
        self.pack_start(bar, False, False, 0)
        self.pack_start(scroll, True, True, 0)
        self.refresh()
        GLib.timeout_add_seconds(JIRA_POLL, lambda: self.refresh() or True)

    # -- the list ------------------------------------------------------------

    def linked(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for e in self.win.entries.values():
            for k in e.get("tickets") or []:
                out.setdefault(k, []).append(e["name"])
        return out

    def refresh(self):
        self.count.set_text("loading…")
        run_bg(self.win, "jira", lambda: self.win.jira.Jira().search(), self.show)

    def show(self, issues: list[dict]):
        self.issues = issues
        links = self.linked()
        keys = []
        for i, issue in enumerate(issues):
            keys.append(issue["key"])
            row = self.rows.get(issue["key"])
            if row is None:
                row = self.rows[issue["key"]] = TicketRow(issue)
                self.list.insert(row, i)
            elif row.get_index() != i:
                self.list.remove(row)
                self.list.insert(row, i)
            row.update(issue, links.get(issue["key"], []))
        for k in set(self.rows) - set(keys):
            self.list.remove(self.rows.pop(k))
        self.count.set_text(f"{len(issues)} ticket{'s' if len(issues) != 1 else ''}")

    def relink(self):
        """Linked projects changed (the registry was re-read)."""
        links = self.linked()
        for k, row in self.rows.items():
            row.update(row.issue, links.get(k, []))

    # -- what can be done with one ------------------------------------------

    def actions(self, row: TicketRow):
        """The ticket's menu, once its transitions are known."""
        issue = row.issue

        def work():
            # A ticket that can't be moved (or a Jira that won't say) still
            # gets the rest of its menu.
            try:
                return self.win.jira.Jira().transitions(issue["key"])
            except self.win.jira.JiraError as exc:
                GLib.idle_add(self.win.status.set_text, f"{issue['key']}: {exc}"[:120])
                return []
        run_bg(self.win, issue["key"], work, lambda ts: self.menu(row, ts))

    def menu(self, row: TicketRow, transitions: list[dict]):
        issue, win = row.issue, self.win
        key = issue["key"]
        linked = self.linked().get(key, [])
        menu = Gtk.Menu()

        def item(label, act=None, sub=None, sensitive=True):
            mi = Gtk.MenuItem(label=label)
            if sub is not None:
                mi.set_submenu(sub)
            elif act is not None:
                mi.connect("activate", lambda *_: act())
            mi.set_sensitive(sensitive)
            menu.append(mi)
            return mi

        def submenu(pairs):
            sm = Gtk.Menu()
            for label, act in pairs:
                mi = Gtk.MenuItem(label=label)
                mi.connect("activate", lambda *_, a=act: a())
                sm.append(mi)
            return sm

        group = linked[0] if linked else None
        item("Open in a tab", lambda: win.open_page(None, issue["url"], group=group))
        item("Open in the browser",
             lambda: Gtk.show_uri_on_window(win, issue["url"], Gdk.CURRENT_TIME))
        menu.append(Gtk.SeparatorMenuItem())
        item("Start work in a new worktree…", lambda: new_worktree(win, ticket=issue))
        live = [n for n in linked if any(a["up"] for a in
                                         (win.entries.get(n) or {}).get("agents", {}).values())]
        if live:
            item("Send to agent", sub=submenu([(n, lambda n=n: self.send(issue, n)) for n in live]))
        others = [n for n in sorted(win.entries) if n not in linked]
        if others:
            item("Link to project", sub=submenu(
                [(n, lambda n=n: self.link(n, key)) for n in others]))
        explicit = [n for n in linked if key in ((win.entries.get(n) or {}).get("jira") or [])]
        for n in explicit:
            item(f"Unlink from {n}", lambda n=n: self.link(n, key, unlink=True))
        menu.append(Gtk.SeparatorMenuItem())
        if transitions:
            item("Move to", sub=submenu(
                [(t["to"] if t["to"] == t["name"] else f"{t['to']}  ({t['name']})",
                  lambda t=t: self.move(key, t["id"], t["to"])) for t in transitions]))
        item("Comment…", lambda: self.comment(key))
        item("Assign to me", lambda: run_bg(
            win, key, lambda: win.jira.Jira().assign_to_me(key),
            lambda _: (win.status.set_text(f"{key} assigned to you"), self.refresh())))
        menu.show_all()
        menu.attach_to_widget(row, None)
        menu.popup_at_widget(row, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)

    def link(self, name: str, key: str, unlink: bool = False):
        run_bg(self.win, "link", lambda: self.win.registry.link_ticket(name, key, unlink=unlink),
               lambda _: (self.win.status.set_text(
                   f"{key} {'unlinked from' if unlink else 'linked to'} {name}"),
                   self.win.refresh_projects()))

    def move(self, key: str, tid: str, to: str):
        run_bg(self.win, key, lambda: self.win.jira.Jira().transition(key, tid),
               lambda _: (self.win.status.set_text(f"{key} → {to}"), self.refresh()))

    def comment(self, key: str):
        v = form(self.win, f"Comment on {key}", [("text", "Comment", "text", "")], ok="Comment")
        if v and v["text"].strip():
            run_bg(self.win, key, lambda: self.win.jira.Jira().comment(key, v["text"].strip()),
                   lambda _: self.win.status.set_text(f"commented on {key}"))

    def send(self, issue: dict, name: str):
        """The ticket, in full, as a prompt to the project's running agent."""
        win = self.win
        e = win.entries.get(name) or {}
        agent = next((a for a, st in e.get("agents", {}).items() if st["up"]), None)
        if agent is None or not win.confirm(
                f"Send {issue['key']} to {name}'s {agent}?",
                "Its summary, description and link are pasted into the agent's "
                "session and submitted."):
            return

        def work():
            full = win.jira.Jira().issue(issue["key"])
            text = (f"Work on {full['key']}: {full['summary']}\n\n"
                    f"{full['description'].strip()}\n\n{full['url']}").strip()
            st = win.agent_status
            target = st.get_instance(st.get_project(name), agent)["target"]
            st.send_to_project(target, text)
        run_bg(win, "send", work, lambda _: (
            win.status.set_text(f"sent {issue['key']} to {name}"),
            win.open_agent(name, agent)))

    def new_ticket(self):
        win, s = self.win, self.win.jira.settings()
        projects = [("", "(none)")] + [(n, n) for n in sorted(win.entries)]
        v = form(win, "New ticket", [
            ("summary", "Summary", "entry", ""),
            ("desc", "Description", "text", ""),
            ("project", "Jira project", "entry", s["default_project"]),
            ("type", "Type", "entry", s["issue_type"]),
            ("link", "Link to", "combo", "", projects),
            ("assign", "Assign to me", "check", True),
        ], ok="Create")
        if not v or not v["summary"]:
            return

        def work():
            key = win.jira.Jira().create(v["summary"], v["desc"], project=v["project"] or None,
                                         issue_type=v["type"] or None, assign_me=v["assign"])
            if v["link"]:
                win.registry.link_ticket(v["link"], key)
            return key
        run_bg(win, "new ticket", work, lambda key: (
            win.status.set_text(f"created {key}"), win.refresh_projects(), self.refresh()))

"""The window's CPU tab: the reservation queue (reserve/) and its controls.

What the server's own page shows — running and waiting jobs, what finished
lately — with the same controls: stop a running job; move a waiter to the
front, change its cores or cancel it; pause granting; change how many cores
are kept free. Read through core/cpu.py, so the voice server need not run.
"""

from __future__ import annotations

import json
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

import cpu  # noqa: E402
from tickets import run_bg  # noqa: E402

POLL = 3   # seconds
HOW = {"done": "done", "stopped": "stopped", "cancelled": "cancelled",
       "lapsed": "client vanished"}


def ago(t: float | None) -> str:
    s = max(0, time.time() - (t or time.time()))
    return "now" if s < 60 else f"{int(s // 60)}m" if s < 3600 else f"{int(s // 3600)}h {int(s % 3600 // 60)}m"


def cores(n: int) -> str:
    return f"{n} core{'' if n == 1 else 's'}"


class CpuView(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.state: dict | None = None
        self.sig = None
        # Two lines: the side panel is narrow.
        top = Gtk.Box(spacing=6, margin=6, margin_bottom=2)
        bar = Gtk.Box(spacing=6, margin=6, margin_top=0)
        self.summary = Gtk.Label(xalign=0)
        self.summary.set_ellipsize(Pango.EllipsizeMode.END)
        self.level = Gtk.LevelBar(min_value=0, max_value=1, hexpand=True, valign=Gtk.Align.CENTER)
        self.pause = Gtk.Button()
        self.pause.connect("clicked", lambda *_: self.post(
            "pause", lambda: cpu.settings(paused=not self.state["paused"])))
        keep = Gtk.Box(spacing=2)
        keep.get_style_context().add_class("linked")
        self.keep_lbl = Gtk.Label()
        minus = Gtk.Button(label="−")
        plus = Gtk.Button(label="+")
        minus.connect("clicked", lambda *_: self.keep(-1))
        plus.connect("clicked", lambda *_: self.keep(1))
        keep.set_tooltip_text("Cores never booked, for the desktop and the agents")
        for w in (minus, self.keep_lbl, plus):
            keep.pack_start(w, False, False, 0)
        top.pack_start(self.summary, False, False, 0)
        top.pack_start(self.level, True, True, 0)
        bar.pack_start(self.pause, False, False, 0)
        bar.pack_end(keep, False, False, 0)
        bar.pack_end(Gtk.Label(label="kept free"), False, False, 0)
        self.note = Gtk.Label(xalign=0, margin_start=8, margin_bottom=4)
        self.note.get_style_context().add_class("dim-label")
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list.set_header_func(self.header)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.add(self.list)
        self.pack_start(top, False, False, 0)
        self.pack_start(bar, False, False, 0)
        self.pack_start(self.note, False, False, 0)
        self.pack_start(scroll, True, True, 0)
        self.refresh()
        GLib.timeout_add_seconds(POLL, lambda: self.refresh() or True)

    # -- reading -------------------------------------------------------------

    def refresh(self):
        run_bg(self.win, "cpu queue", cpu.state, self.show)

    def show(self, s: dict):
        self.state = s
        run = [e for e in s["entries"] if e["state"] == "granted"]
        wait = [e for e in s["entries"] if e["state"] == "waiting"]
        self.summary.set_markup(
            f"<b>{s['booked']}/{s['capacity']}</b> cores booked"
            + (f" · {len(wait)} waiting" if wait else "")
            + (' · <span foreground="#e5a50a"><b>paused</b></span>' if s["paused"] else ""))
        self.level.set_value(s["booked"] / max(1, s["capacity"]))
        self.pause.set_label("▶ Resume" if s["paused"] else "⏸ Pause")
        self.pause.set_tooltip_text("Start granting again" if s["paused"] else
                                    "Hold the queue: running jobs carry on, nothing new starts")
        self.keep_lbl.set_text(f" {s['keep_free']} ")
        self.note.set_text(f"load {s['load']:.0f} on {s['cores']} cores"
                           + (f" · CPU pressure {s['pressure']:.0f}%" if s.get("pressure") is not None else ""))
        sig = json.dumps([[(e["id"], e["state"], e["cores"], e.get("position"), e.get("stop"))
                           for e in s["entries"]], [h["id"] for h in s.get("history", [])[:8]]])
        if sig != self.sig:
            self.sig = sig
            self.rebuild(run, wait, s.get("history", [])[:8])
        else:   # only the times move
            for row in self.list.get_children():
                row.tick()

    def rebuild(self, run, wait, hist):
        for row in self.list.get_children():
            self.list.remove(row)
        for e in run:
            self.list.add(JobRow(self, e, "Running"))
        for e in wait:
            self.list.add(JobRow(self, e, "Waiting"))
        for h in hist:
            self.list.add(JobRow(self, h, "Finished lately"))
        if not run and not wait:
            self.list.add(JobRow(self, None, "Running"))
        self.list.show_all()

    def header(self, row, before):
        if before is not None and before.section == row.section:
            row.set_header(None)
            return
        lbl = Gtk.Label(xalign=0, margin_start=8, margin_top=8)
        lbl.set_markup(f"<small><b>{GLib.markup_escape_text(row.section)}</b></small>")
        row.set_header(lbl)

    # -- steering ------------------------------------------------------------

    def post(self, what: str, work):
        run_bg(self.win, what, lambda: (work(), cpu.state())[1], self.show)

    def keep(self, delta: int):
        if self.state:
            n = self.state["keep_free"] + delta
            self.post("kept free", lambda: cpu.settings(keep_free=n))

    def stop(self, e: dict):
        if confirm(self.win, f"Stop {e['project']}'s {cores(e['cores'])} job?",
                   f"{e['label'] or e['command']}\n\nIts command gets SIGTERM, then SIGKILL if it lingers."):
            self.post("stop", lambda: cpu.act("stop", e["id"]))

    def resize(self, e: dict):
        n = ask_cores(self.win, e, self.state["capacity"])
        if n:
            self.post("cores", lambda: cpu.act("cores", e["id"], {"cores": n}))


class JobRow(Gtk.ListBoxRow):
    def __init__(self, view: CpuView, e: dict | None, section: str):
        super().__init__()
        self.set_activatable(False)
        self.section = section
        self.e = e
        box = Gtk.Box(spacing=8, margin=6, margin_start=10)
        if e is None:
            lbl = Gtk.Label(label="Nothing booked and nobody waiting.", xalign=0)
            lbl.get_style_context().add_class("dim-label")
            box.pack_start(lbl, True, True, 0)
            self.add(box)
            self.sub = None
            return
        n = Gtk.Label(xalign=0, width_chars=8)
        colour = {"Running": "#2ec27e", "Waiting": "#e5a50a"}.get(section)
        n.set_markup(f'<span foreground="{colour}"><b>{cores(e["cores"])}</b></span>' if colour
                     else GLib.markup_escape_text(cores(e["cores"])))
        what = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title = Gtk.Label(xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_markup(f"<b>{GLib.markup_escape_text(e['project'])}</b> "
                         f"{GLib.markup_escape_text(e.get('label') or '')}")
        self.sub = Gtk.Label(xalign=0)
        self.sub.set_ellipsize(Pango.EllipsizeMode.END)
        self.sub.get_style_context().add_class("dim-label")
        what.pack_start(title, False, False, 0)
        what.pack_start(self.sub, False, False, 0)
        self.set_tooltip_text(e.get("command") or None)
        box.pack_start(n, False, False, 0)
        box.pack_start(what, True, True, 0)
        if section == "Running":
            if e.get("stop"):
                box.pack_end(Gtk.Label(label="stopping…"), False, False, 0)
            else:
                box.pack_end(button("Stop", lambda: view.stop(e), "destructive-action"), False, False, 0)
        elif section == "Waiting":
            # A line of their own: three buttons beside the job leave the
            # narrow side panel no room to say what the job is.
            acts = Gtk.Box(spacing=6, halign=Gtk.Align.END)
            if e.get("position", 1) > 1:
                acts.pack_start(button("Front", lambda: view.post(
                    "front", lambda: cpu.act("front", e["id"]))), False, False, 0)
            acts.pack_start(button("Cores", lambda: view.resize(e)), False, False, 0)
            acts.pack_start(button("Cancel", lambda: view.post(
                "cancel", lambda: cpu.act("cancel", e["id"])), "destructive-action"), False, False, 0)
            what.pack_start(acts, False, False, 4)
        self.add(box)
        self.tick()

    def tick(self):
        if self.sub is None:
            return
        e = self.e
        if self.section == "Running":
            bits = [f"running {ago(e['granted'])}"]
            if e.get("where") and e["where"] != "host":
                bits.append(f"in {e['where']}")
        elif self.section == "Waiting":
            bits = [f"#{e['position']}", f"waiting {ago(e['requested'])}"]
        else:
            bits = [HOW.get(e["how"], e["how"])]
            if e.get("code"):
                bits.append(f"exit {e['code']}")
            when = ago(e["ended"])
            bits.append("just now" if when == "now" else f"{when} ago")
        bits.append(e.get("command") or "")
        self.sub.set_text(" · ".join(bits))


def button(label: str, fn, style: str | None = None) -> Gtk.Button:
    b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    if style:
        b.get_style_context().add_class(style)
    b.connect("clicked", lambda *_: fn())
    return b


def confirm(win, title: str, detail: str) -> bool:
    d = Gtk.MessageDialog(transient_for=win, modal=True, message_type=Gtk.MessageType.WARNING,
                          buttons=Gtk.ButtonsType.OK_CANCEL, text=title)
    d.format_secondary_text(detail)
    ok = d.run() == Gtk.ResponseType.OK
    d.destroy()
    return ok


def ask_cores(win, e: dict, capacity: int) -> int | None:
    d = Gtk.Dialog(title=f"Cores for {e['project']}", transient_for=win, modal=True)
    d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Set", Gtk.ResponseType.OK)
    d.set_default_response(Gtk.ResponseType.OK)
    spin = Gtk.SpinButton.new_with_range(1, capacity, 1)
    spin.set_value(e["cores"])
    spin.set_activates_default(True)
    box = d.get_content_area()
    box.set_spacing(6)
    box.set_border_width(10)
    box.add(Gtk.Label(label=e.get("label") or e.get("command") or "", xalign=0))
    box.add(spin)
    d.show_all()
    ok = d.run() == Gtk.ResponseType.OK
    n = spin.get_value_as_int()
    d.destroy()
    return n if ok else None

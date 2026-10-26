#!/usr/bin/env python3
"""Tests for desktop/'s pure helpers: link hosts and the certificate trust
they decide, the address a project links to, the env an embedded terminal
starts with, and the little formatters the window and the CPU view show.
GTK is imported but no window is made; if the bindings aren't installed the
whole module is skipped. Config, projects and state point at a scratch dir.

    python3 desktop/test_desktop.py
"""

import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

SCRATCH = tempfile.mkdtemp(prefix="radio-room-test-")
os.environ.setdefault("VP_EVENTS_DB", str(Path(SCRATCH) / "events.db"))
os.environ["XDG_STATE_HOME"] = str(Path(SCRATCH) / "state")
os.environ.setdefault("RADIO_ROOM_CONFIG", str(Path(SCRATCH) / "config.toml"))
_PROJECTS_WAS = os.environ.get("RADIO_ROOM_PROJECTS")
os.environ["RADIO_ROOM_PROJECTS"] = str(Path(SCRATCH) / "projects.toml")
if not Path(os.environ["RADIO_ROOM_CONFIG"]).exists():
    Path(os.environ["RADIO_ROOM_CONFIG"]).write_text(
        f'projects = "{SCRATCH}/projects.toml"\nagents = ["claude"]\n')

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import radio_room  # noqa: E402
    import cpu_view  # noqa: E402
    import tickets  # noqa: E402
    SKIP = None
except (ImportError, ValueError) as e:   # no gi, or no Gtk/Vte/WebKit2 typelib
    SKIP = f"GTK bindings unavailable: {e}"


def _forget_core_modules():
    """The registry reads its paths once, at import. Drop core/'s modules (ours
    keep their references) and our projects override, so a test file imported
    after this one, core/test_core.py, gets them fresh with its own config."""
    for name in ("config", "registry", "git", "jira", "cpu", "pastes", "listen"):
        sys.modules.pop(name, None)
    if _PROJECTS_WAS is None:
        os.environ.pop("RADIO_ROOM_PROJECTS", None)
    else:
        os.environ["RADIO_ROOM_PROJECTS"] = _PROJECTS_WAS


_forget_core_modules()


@unittest.skipIf(SKIP, SKIP)
class Links(unittest.TestCase):
    def test_host_of_a_url(self):
        self.assertEqual(radio_room.host_of("https://box.lan:8443/x?y=1"), "box.lan")
        self.assertEqual(radio_room.host_of("http://127.0.0.1/"), "127.0.0.1")
        self.assertIsNone(radio_room.host_of("not a url"))

    def test_trusted_hosts_are_this_machine_and_the_registered_services(self):
        win = types.SimpleNamespace(entries={
            "a": {"host": "box.lan"}, "b": {"host": ""}, "c": {}, "d": {"host": "nas.lan"}})
        trusted = radio_room.Window.trusted_hosts(win)
        self.assertEqual(trusted, {"localhost", "127.0.0.1", "box.lan", "nas.lan"})
        self.assertNotIn(radio_room.host_of("https://evil.example/"), trusted)
        self.assertIn(radio_room.host_of("https://box.lan:9000/page"), trusted)

    def test_a_project_links_to_its_fixed_url_or_its_host_and_port(self):
        e = {"url": "", "scheme": "https", "host": "box.lan", "port": 8443, "path": "/app"}
        self.assertEqual(radio_room.web_url(e), "https://box.lan:8443/app")
        self.assertEqual(radio_room.web_url({**e, "host": ""}), "https://localhost:8443/app")
        self.assertEqual(radio_room.web_url({**e, "url": "https://x.ts.net/"}), "https://x.ts.net/")


@unittest.skipIf(SKIP, SKIP)
class TerminalEnv(unittest.TestCase):
    def test_tmux_and_ssh_variables_are_dropped_before_the_assignments(self):
        argv = radio_room.clean_argv(["tmux", "attach"], {"FOO": "bar"})
        self.assertEqual(argv[0], "env")
        self.assertEqual(argv[-2:], ["tmux", "attach"])
        unset = [argv[i + 1] for i, a in enumerate(argv) if a == "-u"]
        self.assertEqual(set(unset), {"TMUX", "TMUX_PANE", "SSH_CLIENT_WEB",
                                      "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"})
        sets = [a for a in argv if "=" in a]
        self.assertEqual(sets, ["TERM=xterm-256color", "FOO=bar"])
        self.assertLess(argv.index("-u"), argv.index("TERM=xterm-256color"))

    def test_the_caller_can_override_term(self):
        argv = radio_room.clean_argv(["bash"], {"TERM": "dumb"})
        self.assertEqual([a for a in argv if a.startswith("TERM=")], ["TERM=dumb"])


@unittest.skipIf(SKIP, SKIP)
class Formatting(unittest.TestCase):
    def test_token_counts_read_short(self):
        cases = {None: "0", 999: "999", 1500: "1.5k", 15_000: "15k", 2_500_000: "2.5M",
                 25_000_000: "25M", 3_200_000_000: "3.2B", 40_000_000_000: "40B"}
        for n, want in cases.items():
            self.assertEqual(radio_room.fmt_tokens(n), want, n)

    def test_until_a_quota_reset(self):
        soon = (datetime.now() + timedelta(hours=3, minutes=20, seconds=30)).isoformat()
        later = (datetime.now() + timedelta(days=2, hours=5, seconds=30)).isoformat()
        minutes = (datetime.now() + timedelta(minutes=7, seconds=30)).isoformat()
        self.assertEqual(radio_room.until(soon), "in 3h 20m")
        self.assertEqual(radio_room.until(later), "in 2d 5h")
        self.assertEqual(radio_room.until(minutes), "in 7m")
        self.assertEqual(radio_room.until("2001-01-01T00:00:00"), "now")
        self.assertEqual(radio_room.until(None), "?")
        self.assertEqual(radio_room.until("soonish"), "soonish")

    def test_memory_in_gib(self):
        self.assertEqual(radio_room.gib(3 * 2**30 + 2**29), "3.5G")

    def test_how_long_ago_a_job_ended(self):
        now = time.time()
        self.assertEqual(cpu_view.ago(now - 5), "now")
        self.assertEqual(cpu_view.ago(now - 125), "2m")
        self.assertEqual(cpu_view.ago(now - 2 * 3600 - 300), "2h 5m")
        self.assertEqual(cpu_view.ago(None), "now")
        self.assertEqual(cpu_view.ago(now + 100), "now")

    def test_core_counts_are_pluralised(self):
        self.assertEqual((cpu_view.cores(1), cpu_view.cores(4)), ("1 core", "4 cores"))


@unittest.skipIf(SKIP, SKIP)
class Worktrees(unittest.TestCase):
    def test_each_repository_is_offered_once_from_its_main_checkout(self):
        entries = {
            "wt": {"dir": "/src/app.wt/feat", "git": {"repo": "/src/app/.git", "name": "app",
                                                      "worktree": True}},
            "app": {"dir": "/src/app", "git": {"repo": "/src/app/.git", "name": "app"}},
            "Lib": {"dir": "/src/lib", "git": {"repo": "/src/lib/.git", "name": "Lib"}},
            "plain": {"dir": "/src/plain", "git": None},
        }
        self.assertEqual(tickets.repos(entries), [("/src/app/.git", "app", "/src/app"),
                                                 ("/src/lib/.git", "Lib", "/src/lib")])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for the hand-rolled parts: run with `./test_ssh_client.py`.

Only the pieces where being subtly wrong is quiet rather than loud — a bad
handshake reply looks like a working server that browsers silently refuse, and
a mis-split history looks like a terminal that redraws wrong once in a while.
"""

import os
import socket
import threading
import time
import unittest

import session
import tabs
import wsproto


class AcceptKey(unittest.TestCase):
    def test_rfc6455_vector(self):
        # Section 1.3 of RFC 6455. A wrong GUID still produces a plausible
        # base64 accept value and a 101 response, and every browser drops the
        # connection without ever calling onopen — so this vector is the only
        # thing standing between that and a long debugging session.
        self.assertEqual(
            wsproto.accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )


class Framing(unittest.TestCase):
    """Round-trip through a real socket pair, so lengths and masking are real."""

    def round_trip(self, payload, opcode=wsproto.OP_TEXT):
        client, server = socket.socketpair()
        ws = wsproto.WebSocket(server)

        mask = b"\xa1\xb2\xc3\xd4"
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < (1 << 16):
            header = bytes([0x80 | opcode, 0x80 | 126]) + length.to_bytes(2, "big")
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + length.to_bytes(8, "big")
        client.sendall(header + mask + masked)

        got = ws.recv()
        client.close()
        server.close()
        return got

    def test_short_frame(self):
        self.assertEqual(self.round_trip(b"hello"), (wsproto.OP_TEXT, b"hello"))

    def test_16_bit_length(self):
        payload = os.urandom(5000)
        self.assertEqual(self.round_trip(payload, wsproto.OP_BINARY),
                         (wsproto.OP_BINARY, payload))

    def test_64_bit_length(self):
        payload = os.urandom(70000)
        self.assertEqual(self.round_trip(payload, wsproto.OP_BINARY),
                         (wsproto.OP_BINARY, payload))

    def test_unmasked_client_frame_is_rejected(self):
        client, server = socket.socketpair()
        ws = wsproto.WebSocket(server)
        client.sendall(bytes([0x81, 5]) + b"hello")
        with self.assertRaises(wsproto.WebSocketError):
            ws.recv()
        client.close()
        server.close()

    def test_fragmented_message_is_reassembled(self):
        client, server = socket.socketpair()
        ws = wsproto.WebSocket(server)
        mask = b"\x00\x00\x00\x00"  # a legal mask, and an easy one
        client.sendall(bytes([0x01, 0x80 | 3]) + mask + b"abc")   # text, not final
        client.sendall(bytes([0x00, 0x80 | 3]) + mask + b"def")   # continuation
        client.sendall(bytes([0x80, 0x80 | 3]) + mask + b"ghi")   # final
        self.assertEqual(ws.recv(), (wsproto.OP_TEXT, b"abcdefghi"))
        client.close()
        server.close()

    def test_ping_is_answered_and_hidden(self):
        client, server = socket.socketpair()
        ws = wsproto.WebSocket(server)
        mask = b"\x00\x00\x00\x00"
        client.sendall(bytes([0x89, 0x80]) + mask)                # ping
        client.sendall(bytes([0x81, 0x80 | 2]) + mask + b"hi")    # then a message
        self.assertEqual(ws.recv(), (wsproto.OP_TEXT, b"hi"))     # ping not surfaced
        reply = client.recv(64)
        self.assertEqual(reply[0] & 0x0F, wsproto.OP_PONG)
        client.close()
        server.close()


class HistoryRouting(unittest.TestCase):
    """The main/alt split that decides what a reconnecting browser is sent."""

    def blank(self):
        s = session.PtySession.__new__(session.PtySession)
        s._main, s._alt = bytearray(), bytearray()
        s._alt_mode, s._pending = False, b""
        s._lock = threading.Lock()
        return s

    def test_alt_output_is_kept_out_of_scrollback(self):
        s = self.blank()
        s._record(b"$ ")
        s._record(b"\x1b[?1049hFRAME")
        self.assertEqual(s._replay(), b"$ \x1b[?1049hFRAME")
        s._record(b"\x1b[?1049lback")
        # Everything the TUI drew is gone; the shell's scrollback is intact.
        self.assertEqual(s._replay(), b"$ back")

    def test_switch_split_across_two_reads(self):
        s = self.blank()
        s._record(b"A\x1b[?10")
        self.assertEqual(s._replay(), b"A")   # the fragment is withheld, not filed
        s._record(b"49hFRAME")
        self.assertEqual(s._replay(), b"A\x1b[?1049hFRAME")

    def test_fragment_that_was_not_a_switch_is_not_swallowed(self):
        s = self.blank()
        s._record(b"X\x1b[?4")
        s._record(b"7;5Hhere")
        self.assertEqual(s._replay(), b"X\x1b[?47;5Hhere")

    def test_reentering_alt_drops_the_previous_frame(self):
        s = self.blank()
        s._record(b"\x1b[?1049hOLD\x1b[?1049l\x1b[?1049hNEW")
        self.assertEqual(s._replay(), b"\x1b[?1049hNEW")

    def test_scrollback_is_trimmed_from_the_front(self):
        s = self.blank()
        s._record(b"o" * (session.MAIN_BYTES + 4096) + b"END")
        self.assertEqual(len(s._main), session.MAIN_BYTES)
        self.assertTrue(s._replay().endswith(b"END"))


class LiveSession(unittest.TestCase):
    """A real PTY, so fork/exec, sizing and the exit path are all exercised."""

    def test_runs_resizes_and_reports_its_own_exit(self):
        received = []
        done = threading.Event()

        s = session.PtySession(
            argv=["/bin/bash", "-c", "printf 'ready\\n'; read line; printf 'got:%s\\n' \"$line\""],
            env={"TERM": "xterm-256color", "PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]},
            cwd="/tmp",
            cols=100,
            rows=30,
        )

        def sink(data):
            if data is None:
                done.set()
            else:
                received.append(data)

        # subscribe() returns everything printed before this moment, which for a
        # process that starts talking immediately is most of it.
        received.append(s.subscribe(sink))
        deadline = time.monotonic() + 5
        while b"ready" not in b"".join(received) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn(b"ready", b"".join(received))

        self.assertTrue(s.alive)
        s.resize(120, 40)
        self.assertEqual((s.cols, s.rows), (120, 40))

        s.write(b"hello\n")
        deadline = time.monotonic() + 5
        while b"got:hello" not in b"".join(received) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn(b"got:hello", b"".join(received))

        self.assertTrue(done.wait(5), "the exit sentinel never reached the subscriber")
        self.assertFalse(s.alive)
        self.assertEqual(s.exit_code, 0)

    def test_restart_replaces_a_dead_shell(self):
        s = session.PtySession(
            argv=["/bin/bash", "-c", "exit 3"],
            env={"TERM": "xterm-256color", "PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]},
            cwd="/tmp",
        )
        deadline = time.monotonic() + 5
        while s.alive and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(s.exit_code, 3)

        s.argv = ["/bin/bash", "-c", "sleep 30"]
        s.restart()
        self.assertTrue(s.alive)
        pid = s.pid
        s.restart()
        self.assertTrue(s.alive)
        self.assertNotEqual(s.pid, pid)
        os.kill(s.pid, 9)


class AttachTab(unittest.TestCase):
    def test_restart_goes_back_to_the_plain_shell(self):
        s = session.PtySession(
            argv=["/bin/bash", "-c", "sleep 60"],
            env={"TERM": "xterm-256color", "PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]},
            cwd="/tmp", restart_argv=["/bin/bash", "-c", "sleep 62"])
        self.addCleanup(s.stop)
        s.restart()
        self.assertEqual(s.argv, ["/bin/bash", "-c", "sleep 62"])
        self.assertTrue(s.alive)


class Naming(unittest.TestCase):
    """What a tab decides to call itself, given a foreground job."""

    def test_launcher_wins_wherever_it_sits(self):
        # It is never argv[0] — it runs under an interpreter — and it is never
        # the outermost process, because ~/.bashrc is what starts it.
        chain = [["-bash"], ["/home/x/scripts/.venv/bin/python3", "/home/x/scripts/launcher.py"]]
        self.assertEqual(tabs.name_from_chain(chain), "launcher")

    def test_a_bare_shell_is_called_shell(self):
        self.assertEqual(tabs.name_from_chain([["-bash"]]), "shell")
        self.assertEqual(tabs.name_from_chain([["/bin/bash", "-li"]]), "shell")

    def test_the_outermost_program_names_the_tab(self):
        # Not the innermost: a program's own helpers are still that program,
        # and naming a tab after them would make it flicker as they come and go.
        self.assertEqual(
            tabs.name_from_chain([["-bash"], ["claude"], ["/usr/bin/node", "worker.js"]]),
            "claude",
        )

    def test_nothing_readable_is_not_guessed_at(self):
        self.assertEqual(tabs.name_from_chain([]), tabs.UNKNOWN)


class Tabs(unittest.TestCase):
    """The list itself: opening, switching, closing, and who gets told."""

    def spawn(self, command="sleep 60"):
        return session.PtySession(
            argv=["/bin/bash", "-c", command],
            env={"TERM": "xterm-256color", "PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]},
            cwd="/tmp",
        )

    def setUp(self):
        self.set = tabs.TabSet(self.spawn)
        self.addCleanup(lambda: [t.session.stop() for t in self.set.all()])

    def test_starts_with_one_tab_in_front(self):
        self.assertEqual(len(self.set.all()), 1)
        self.assertEqual(self.set.active, self.set.all()[0].id)

    def test_opening_puts_the_new_tab_in_front(self):
        opened = self.set.open()
        self.assertEqual(self.set.active, opened.id)
        self.assertEqual([t.id for t in self.set.all()][-1], opened.id)

    def test_every_listener_hears_about_a_change(self):
        heard = []
        self.set.listen(heard.append)
        opened = self.set.open()
        self.assertTrue(heard)
        self.assertEqual(heard[-1]["active"], opened.id)
        self.assertEqual(len(heard[-1]["tabs"]), 2)
        # Listeners are registered as bound methods — Connection hands over its
        # own send_text — so dropping one depends on two separately-created
        # bound methods comparing and hashing equal. They do, and the same is
        # true of the output sinks in session.py.
        self.set.unlisten(heard.append)
        self.set.open()
        self.assertEqual(len(heard), 1)

    def test_closing_the_front_tab_hands_over_to_its_neighbour(self):
        first = self.set.all()[0]
        middle = self.set.open()
        last = self.set.open()
        self.assertEqual(self.set.active, last.id)
        self.set.close(last.id)
        # The one that slid into its place, which is the tab to its left.
        self.assertEqual(self.set.active, middle.id)
        self.set.close(middle.id)
        self.assertEqual(self.set.active, first.id)

    def test_closing_a_background_tab_leaves_the_front_alone(self):
        background = self.set.open()
        front = self.set.open()
        self.assertTrue(self.set.close(background.id))
        self.assertEqual(self.set.active, front.id)

    def test_the_last_tab_stays(self):
        only = self.set.all()[0]
        self.assertFalse(self.set.close(only.id))
        self.assertEqual(len(self.set.all()), 1)
        self.assertTrue(only.session.alive)

    def test_a_tab_can_be_opened_on_a_command(self):
        opened = self.set.open("sleep 61")
        self.assertEqual(opened.session.argv[-1], "sleep 61")
        self.assertEqual(self.set.active, opened.id)

    def test_a_tab_is_found_by_what_it_shows(self):
        opened = self.set.open()
        opened.name = "render"
        self.assertIs(self.set.named("render"), opened)
        self.assertIsNone(self.set.named("garmin"))
        opened.session.stop()
        self.assertIsNone(self.set.named("render"))   # a dead tab is no answer

    def test_it_stops_opening_tabs_eventually(self):
        for _ in range(tabs.MAX_TABS - 1):
            self.assertIsNotNone(self.set.open())
        self.assertIsNone(self.set.open())
        self.assertEqual(len(self.set.all()), tabs.MAX_TABS)


class ForegroundReading(unittest.TestCase):
    """Reading a name off a live PTY, which is where tab names come from."""

    def test_it_sees_past_the_shell_to_the_job_it_is_running(self):
        # The shape ~/.bashrc produces: a child started before job control, so
        # it shares the shell's process group and tcgetpgrp() names the shell.
        s = session.PtySession(
            argv=["/bin/bash", "-c", "exec 2>/dev/null; sleep 60"],
            env={"TERM": "xterm-256color", "PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]},
            cwd="/tmp",
        )
        self.addCleanup(s.stop)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            chain = s.foreground_chain()
            if any("sleep" in argv[0] for argv in chain):
                break
            time.sleep(0.05)
        self.assertTrue(chain, "nothing was readable from the foreground group")
        self.assertEqual(tabs.name_from_chain(chain), "sleep")

    def test_a_dead_session_reads_as_nothing(self):
        s = session.PtySession(
            argv=["/bin/bash", "-c", "exit 0"],
            env={"TERM": "xterm-256color", "PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]},
            cwd="/tmp",
        )
        deadline = time.monotonic() + 5
        while s.alive and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(s.foreground_chain(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

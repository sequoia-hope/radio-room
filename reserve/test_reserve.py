#!/usr/bin/env python3
"""Tests for reserve/server.py: the book's strict first-come-first-served
granting, what the page can do to it (front, shrink, cancel, stop, pause,
keep_free), lapsing clients that went quiet, and the book surviving a
restart. Plus the HTTP handler on an ephemeral loopback port. Nothing here
touches the real book, config or registry.

    python3 reserve/test_reserve.py
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

SCRATCH = tempfile.mkdtemp(prefix="radio-room-test-")
os.environ.setdefault("RADIO_ROOM_CONFIG", str(Path(SCRATCH) / "config.toml"))
_PROJECTS_WAS = os.environ.get("RADIO_ROOM_PROJECTS")
os.environ["RADIO_ROOM_PROJECTS"] = str(Path(SCRATCH) / "projects.toml")
if not Path(os.environ["RADIO_ROOM_CONFIG"]).exists():
    Path(os.environ["RADIO_ROOM_CONFIG"]).write_text(
        f'projects = "{SCRATCH}/projects.toml"\nagents = ["claude"]\n')

# Loaded under its own name: voice/ and terminal/ have a server.py too.
_spec = importlib.util.spec_from_file_location(
    "reserve_server", Path(__file__).resolve().parent / "server.py")
reserve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reserve)


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


class Clock:
    """Stands in for the time module inside reserve, so lapses are exact."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def time(self):
        return self.now


class BookTest(unittest.TestCase):
    def setUp(self):
        self.real_time = reserve.time
        self.clock = Clock()
        reserve.time = types.SimpleNamespace(
            time=self.clock.time, time_ns=lambda: int(self.clock.now * 1e9))
        self.tmp = Path(tempfile.mkdtemp(prefix="reserve-book-"))

    def tearDown(self):
        reserve.time = self.real_time

    def book(self, total=8, keep_free=2, path=None):
        return reserve.Book(total, keep_free, path)

    @staticmethod
    def ask(book, cores, project="p", **kw):
        return book.request({"project": project, "cores": cores, **kw})

    @staticmethod
    def states(book):
        return [(e["project"], e["state"]) for e in book.snapshot()["entries"]]


class Granting(BookTest):
    def test_capacity_is_total_less_keep_free_and_never_below_one(self):
        self.assertEqual(self.book(8, 2).capacity, 6)
        self.assertEqual(self.book(4, 10).capacity, 1)

    def test_a_request_that_fits_is_granted_at_once(self):
        b = self.book()
        e = self.ask(b, 4)
        self.assertEqual(e["state"], "granted")
        self.assertEqual(b.snapshot()["booked"], 4)

    def test_a_request_for_more_than_capacity_is_cut_to_capacity(self):
        b = self.book(8, 2)
        e = self.ask(b, 20)
        self.assertEqual((e["cores"], e["asked"], e["state"]), (6, 20, "granted"))

    def test_nothing_behind_the_head_jumps_ahead_even_when_it_would_fit(self):
        b = self.book(8, 2)
        first = self.ask(b, 4, "a")
        head = self.ask(b, 4, "b")
        small = self.ask(b, 1, "c")
        self.assertEqual(head["state"], "waiting")
        self.assertEqual(small["state"], "waiting")   # 2 cores free, still waits
        self.assertEqual(self.states(b), [("a", "granted"), ("b", "waiting"), ("c", "waiting")])

        b.release(first["id"], 0)
        self.assertEqual(self.states(b), [("b", "granted"), ("c", "granted")])

    def test_a_waiter_sees_its_place_and_who_is_ahead_and_running(self):
        b = self.book(8, 2)
        self.ask(b, 6, "a", label="build")
        self.ask(b, 3, "b")
        e = self.ask(b, 2, "c")
        self.assertEqual(e["position"], 2)
        self.assertEqual([x["project"] for x in e["ahead"]], ["b"])
        self.assertEqual(e["running"], [{"project": "a", "cores": 6, "label": "build"}])
        self.assertFalse(e["paused"])

    def test_release_records_the_job_as_done_with_its_exit_code(self):
        b = self.book()
        e = self.ask(b, 2, "a", label="tests")
        b.release(e["id"], 3)
        h = b.snapshot()["history"][0]
        self.assertEqual((h["id"], h["how"], h["code"], h["label"]), (e["id"], "done", 3, "tests"))
        self.assertEqual(b.snapshot()["entries"], [])

    def test_releasing_a_waiter_leaves_no_history(self):
        b = self.book(8, 2)
        self.ask(b, 6)
        w = self.ask(b, 1)
        b.release(w["id"])
        self.assertEqual(b.snapshot()["history"], [])
        self.assertEqual(len(b.snapshot()["entries"]), 1)

    def test_a_resumed_job_is_granted_even_over_capacity(self):
        b = self.book(8, 2)
        self.ask(b, 6)
        e = self.ask(b, 4, resume=True)
        self.assertEqual(e["state"], "granted")
        self.assertEqual(b.snapshot()["booked"], 10)
        self.assertEqual(self.ask(b, 1)["state"], "waiting")

    def test_a_rebooking_client_goes_back_before_waiters_that_asked_later(self):
        b = self.book(8, 2)
        self.ask(b, 6, "running")
        self.clock.now += 10
        self.ask(b, 2, "later")
        lost = self.ask(b, 2, "lost", requested=self.clock.now - 5)
        self.assertEqual(lost["position"], 1)
        self.assertEqual([p for p, _ in self.states(b)], ["running", "lost", "later"])


class OperatorControls(BookTest):
    def test_front_moves_a_waiter_to_the_head_and_grants_it_if_it_fits(self):
        b = self.book(8, 2)
        self.ask(b, 4, "a")
        self.ask(b, 4, "b")
        c = self.ask(b, 2, "c")
        self.assertTrue(b.front(c["id"]))
        self.assertEqual(self.states(b), [("a", "granted"), ("c", "granted"), ("b", "waiting")])

    def test_front_refuses_a_running_job_and_an_unknown_id(self):
        b = self.book()
        e = self.ask(b, 1)
        self.assertFalse(b.front(e["id"]))
        self.assertFalse(b.front("nope"))

    def test_shrinking_a_waiter_lets_it_fit_now(self):
        b = self.book(8, 2)
        self.ask(b, 4)
        w = self.ask(b, 4)
        self.assertTrue(b.resize(w["id"], 2))
        self.assertEqual(b.checkin(w["id"])["state"], "granted")
        self.assertEqual(b.snapshot()["booked"], 6)

    def test_resize_is_clamped_to_capacity_and_refused_for_a_running_job(self):
        b = self.book(8, 2)
        r = self.ask(b, 6)
        w = self.ask(b, 1)
        self.assertTrue(b.resize(w["id"], 100))
        self.assertEqual(b.checkin(w["id"])["cores"], 6)
        self.assertFalse(b.resize(r["id"], 1))

    def test_cancelling_a_waiter_records_it_and_lets_the_next_one_move_up(self):
        b = self.book(8, 2)
        self.ask(b, 4, "a")
        big = self.ask(b, 4, "b")
        small = self.ask(b, 2, "c")
        self.assertTrue(b.cancel(big["id"]))
        self.assertEqual(self.states(b), [("a", "granted"), ("c", "granted")])
        self.assertEqual(b.snapshot()["history"][0]["how"], "cancelled")
        self.assertEqual(b.checkin(small["id"])["state"], "granted")
        # Its client finds out at its next check-in.
        self.assertEqual(b.checkin(big["id"])["state"], "cancelled")

    def test_a_running_job_cannot_be_cancelled(self):
        b = self.book()
        e = self.ask(b, 1)
        self.assertFalse(b.cancel(e["id"]))
        self.assertEqual(b.checkin(e["id"])["state"], "granted")

    def test_stop_flags_a_running_job_and_its_release_is_recorded_as_stopped(self):
        b = self.book()
        e = self.ask(b, 2)
        got = b.stop(e["id"])
        self.assertTrue(got["stop"])
        self.assertTrue(b.checkin(e["id"])["stop"])
        b.release(e["id"], -15)
        self.assertEqual(b.snapshot()["history"][0]["how"], "stopped")

    def test_stop_refuses_a_waiter(self):
        b = self.book(8, 2)
        self.ask(b, 6)
        w = self.ask(b, 1)
        self.assertIsNone(b.stop(w["id"]))
        self.assertIsNone(b.stop("nope"))

    def test_pausing_holds_new_grants_and_unpausing_releases_them(self):
        b = self.book()
        b.settings(paused=True)
        e = self.ask(b, 1)
        self.assertEqual(e["state"], "waiting")
        self.assertTrue(e["paused"])
        b.settings(paused=False)
        self.assertEqual(b.checkin(e["id"])["state"], "granted")

    def test_raising_keep_free_never_takes_cores_from_a_running_job(self):
        b = self.book(8, 2)
        r = self.ask(b, 6)
        b.settings(keep_free=6)
        self.assertEqual(b.capacity, 2)
        self.assertEqual(b.checkin(r["id"])["state"], "granted")
        w = self.ask(b, 1)
        self.assertEqual(w["state"], "waiting")
        b.release(r["id"])
        self.assertEqual(b.checkin(w["id"])["state"], "granted")

    def test_keep_free_is_clamped_to_leave_at_least_one_core(self):
        b = self.book(8, 2)
        b.settings(keep_free=50)
        self.assertEqual((b.keep_free, b.capacity), (7, 1))
        b.settings(keep_free=-3)
        self.assertEqual((b.keep_free, b.capacity), (0, 8))


class Lapsing(BookTest):
    def test_a_client_that_stops_checking_in_loses_its_cores_to_the_next(self):
        b = self.book(8, 2)
        gone = self.ask(b, 6, "gone")
        alive = self.ask(b, 2, "alive")
        self.clock.now += reserve.LAPSE - 5
        b.checkin(alive["id"])
        self.clock.now += 10      # gone: LAPSE + 5 quiet; alive: 10
        b.reap()
        self.assertIsNone(b.checkin(gone["id"]))
        self.assertEqual(b.checkin(alive["id"])["state"], "granted")
        self.assertEqual(b.snapshot()["history"][0]["how"], "lapsed")

    def test_nothing_lapses_before_its_time(self):
        b = self.book()
        e = self.ask(b, 1)
        self.clock.now += reserve.LAPSE - 1
        b.reap()
        self.assertIsNotNone(b.checkin(e["id"]))

    def test_a_cancelled_entry_is_reaped_without_a_second_history_row(self):
        b = self.book(8, 2)
        self.ask(b, 6)
        w = self.ask(b, 1)
        b.cancel(w["id"])
        self.clock.now += reserve.LAPSE + 1
        b.reap()
        self.assertIsNone(b.checkin(w["id"]))
        self.assertEqual([h["how"] for h in b.snapshot()["history"]], ["lapsed", "cancelled"])


class Persistence(BookTest):
    def test_a_restart_keeps_every_booking_its_id_and_its_place(self):
        path = self.tmp / "state" / "book.json"
        b = self.book(8, 2, path)
        run = self.ask(b, 4, "a")
        w1 = self.ask(b, 4, "b")
        w2 = self.ask(b, 3, "c")
        b.release(run["id"], 0)
        b.settings(paused=True, keep_free=3)

        self.clock.now += 3600    # a new boot, long after; ids must not collide
        again = self.book(8, 2, path)
        again.load(None)
        self.assertEqual([e["id"] for e in again.snapshot()["entries"]], [w1["id"], w2["id"]])
        self.assertEqual(self.states(again), [("b", "granted"), ("c", "waiting")])
        self.assertEqual((again.keep_free, again.paused), (3, True))
        self.assertEqual(again.snapshot()["history"][0]["id"], run["id"])
        self.assertNotIn(self.ask(again, 1)["id"], (w1["id"], w2["id"]))

    def test_saved_bookings_get_a_fresh_lapse_after_a_restart(self):
        path = self.tmp / "book.json"
        b = self.book(8, 2, path)
        e = self.ask(b, 2)
        self.clock.now += reserve.LAPSE * 10   # the server was down a while
        again = self.book(8, 2, path)
        again.load(None)
        again.reap()
        self.assertEqual(again.checkin(e["id"])["state"], "granted")

    def test_keep_free_from_the_command_line_overrides_the_saved_one(self):
        path = self.tmp / "book.json"
        b = self.book(8, 2, path)
        b.settings(keep_free=5)
        again = self.book(8, 2, path)
        again.load(1)
        self.assertEqual(again.keep_free, 1)

    def test_cancelled_entries_are_not_brought_back(self):
        path = self.tmp / "book.json"
        b = self.book(8, 2, path)
        self.ask(b, 6)
        w = self.ask(b, 1)
        b.cancel(w["id"])
        again = self.book(8, 2, path)
        again.load(None)
        self.assertIsNone(again.checkin(w["id"]))

    def test_a_missing_or_corrupt_book_starts_empty(self):
        path = self.tmp / "book.json"
        b = self.book(8, 2, path)
        b.load(None)
        self.assertEqual(b.snapshot()["entries"], [])
        path.write_text("{not json")
        b = self.book(8, 2, path)
        b.load(None)
        self.assertEqual((b.snapshot()["entries"], b.keep_free), ([], 2))


class StubUsage:
    def get(self):
        return {"projects": [{"project": "p", "cores": 1.5, "top": []}], "total": 1.5}


class HTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = type("H", (reserve.Handler,), {
            "book": reserve.Book(8, 2, None), "usage": StubUsage()})
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), cls.handler)   # an ephemeral port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.handler.book = reserve.Book(8, 2, None)

    def call(self, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data,
                                     method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def book_one(self, cores, project="p"):
        return self.call("/api/request", {"project": project, "where": "box", "cores": cores})[1]

    def test_state_carries_the_book_usage_and_load(self):
        self.book_one(2, "a")
        code, s = self.call("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual((s["capacity"], s["booked"]), (6, 2))
        self.assertEqual(s["entries"][0]["project"], "a")
        self.assertEqual(s["usage"]["total"], 1.5)
        self.assertIn("load", s)

    def test_request_checkin_and_release_round_trip(self):
        e = self.book_one(3)
        self.assertEqual(e["state"], "granted")
        self.assertEqual(self.call(f"/api/checkin/{e['id']}", {})[1]["id"], e["id"])
        self.assertEqual(self.call(f"/api/release/{e['id']}", {"code": 0}), (200, {"ok": True}))
        self.assertEqual(self.call(f"/api/checkin/{e['id']}", {})[0], 404)

    def test_cancel_over_http_cancels_a_waiter_but_not_a_running_job(self):
        run = self.book_one(6)
        w = self.book_one(1)
        self.assertEqual(self.call(f"/api/cancel/{run['id']}", {}), (200, {"ok": False}))
        self.assertEqual(self.call(f"/api/cancel/{w['id']}", {}), (200, {"ok": True}))
        self.assertEqual(len(self.call("/api/state")[1]["entries"]), 1)

    def test_stopping_a_job_that_is_not_on_the_host_leaves_it_to_its_client(self):
        e = self.book_one(1)
        self.assertEqual(self.call(f"/api/stop/{e['id']}", {}), (200, {"ok": True, "scope": False}))

    def test_settings_change_keep_free_and_return_the_new_book(self):
        code, s = self.call("/api/settings", {"keep_free": 4, "paused": True})
        self.assertEqual((code, s["keep_free"], s["capacity"], s["paused"]), (200, 4, 4, True))

    def test_bad_json_and_unknown_paths_are_refused(self):
        self.assertEqual(self.call("/api/request", raw=b"{oops")[0], 400)
        self.assertEqual(self.call("/api/nothing", {})[0], 404)
        self.assertEqual(self.call("/api/nothing")[0], 404)


if __name__ == "__main__":
    unittest.main()

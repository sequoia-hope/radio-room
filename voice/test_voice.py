#!/usr/bin/env python3
"""Tests for voice/: the events store and its state machine, reading an
agent's last word and its pane, the advisor's prompt and tool plumbing, the
conversation store, and the HTTP handler's token check and https redirect on
an ephemeral loopback port. Config, projects and the events database all
point into a scratch directory; nothing talks to Claude, tmux or the network.

    python3 voice/test_voice.py
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SCRATCH = tempfile.mkdtemp(prefix="radio-room-test-")
os.environ["VP_EVENTS_DB"] = str(Path(SCRATCH) / "events.db")
os.environ.setdefault("RADIO_ROOM_CONFIG", str(Path(SCRATCH) / "config.toml"))
_PROJECTS_WAS = os.environ.get("RADIO_ROOM_PROJECTS")
os.environ["RADIO_ROOM_PROJECTS"] = str(Path(SCRATCH) / "projects.toml")
if not Path(os.environ["RADIO_ROOM_CONFIG"]).exists():
    Path(os.environ["RADIO_ROOM_CONFIG"]).write_text(
        f'projects = "{SCRATCH}/projects.toml"\nagents = ["claude"]\n')

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import events  # noqa: E402
import status  # noqa: E402
import summarize  # noqa: E402

# Loaded under its own name: reserve/ and terminal/ have a server.py too.
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("voice_server", HERE / "server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


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


def scratch_dir() -> Path:
    return Path(tempfile.mkdtemp(dir=SCRATCH))


# ── events: heuristics ───────────────────────────────────────────────────────

class AsksQuestion(unittest.TestCase):
    def test_a_question_in_the_last_two_sentences_counts(self):
        self.assertTrue(events.asks_question("Done the refactor. Should I commit it? Or leave it for review."))

    def test_a_question_put_to_you_counts_anywhere_in_the_last_paragraph(self):
        self.assertTrue(events.asks_question(
            "Which file would you like next? You can also skip. I listed them. All six."))

    def test_a_rhetorical_question_answered_in_place_does_not(self):
        self.assertFalse(events.asks_question(
            "Why did it fail? Because the path was wrong. Fixed and rerun."))

    def test_an_ask_without_a_question_mark_counts(self):
        self.assertTrue(events.asks_question("All green.\n\nLet me know if you want the docs too."))
        self.assertTrue(events.asks_question("**Want me to push it**"))

    def test_saying_you_will_tell_them_later_is_not_an_ask(self):
        self.assertFalse(events.asks_question("Deploy started. I'll tell you when it's live."))

    def test_only_the_last_paragraph_is_judged(self):
        self.assertFalse(events.asks_question(
            "Should I use a lock? Yes, I did.\n\nEverything passes now."))

    def test_trailing_markdown_does_not_hide_the_question_mark(self):
        self.assertTrue(events.asks_question("Tests pass. **Merge now?**"))

    def test_an_empty_message_asks_nothing(self):
        self.assertFalse(events.asks_question(""))
        self.assertFalse(events.asks_question(None))


class Transition(unittest.TestCase):
    def t(self, state, event, **info):
        return events.transition(state, event, info)

    def test_a_turn_ending_on_a_question_needs_input_otherwise_completes(self):
        self.assertEqual(self.t("working", "stop", message="Done. Push it?"), "needs_input")
        self.assertEqual(self.t("working", "stop", message="Done and pushed."), "completed")

    def test_session_start_by_source(self):
        self.assertEqual(self.t(None, "session_start", source="startup"), "starting")
        self.assertEqual(self.t("completed", "session_start", source="clear"), "idle")
        self.assertIsNone(self.t("working", "session_start", source="compact"))

    def test_an_idle_notification_ends_a_turn_whose_stop_went_missing(self):
        self.assertEqual(self.t("working", "notify", kind="idle_prompt"), "completed")
        self.assertIsNone(self.t("completed", "notify", kind="idle_prompt"))
        self.assertEqual(self.t("working", "notify", kind="permission_prompt"), "needs_input")

    def test_dismissal_only_clears_what_is_waiting_on_you(self):
        for s in events.DISMISSABLE:
            self.assertEqual(self.t(s, "dismiss"), "idle")
        self.assertIsNone(self.t("working", "dismiss"))
        self.assertIsNone(self.t("offline", "dismiss"))

    def test_pane_events_only_report_a_change(self):
        self.assertIsNone(self.t("working", "busy"))
        self.assertEqual(self.t("completed", "busy"), "working")
        self.assertIsNone(self.t("needs_input", "asking"))
        self.assertEqual(self.t("working", "settled", message="Which one?"), "needs_input")
        self.assertIsNone(self.t("idle", "settled"))
        self.assertEqual(self.t("needs_input", "menu_closed"), "idle")
        self.assertIsNone(self.t("working", "menu_closed"))

    def test_exit_and_revival(self):
        self.assertEqual(self.t("working", "exited"), "offline")
        self.assertIsNone(self.t("offline", "exited"))
        self.assertEqual(self.t("offline", "revived"), "idle")
        self.assertIsNone(self.t("working", "revived"))
        self.assertEqual(self.t("working", "session_end"), "offline")

    def test_an_unknown_event_changes_nothing(self):
        self.assertIsNone(self.t("working", "something_new"))


class Panes(unittest.TestCase):
    def test_a_shell_in_the_pane_means_the_agent_exited(self):
        self.assertEqual(events.classify_pane("claude-code", "bye\nme@box:~/proj$ "), "shell")

    def test_claude_code_asking_busy_and_idle(self):
        self.assertEqual(events.classify_pane("claude-code", "Do you want to proceed?\n❯ 1. Yes"), "asking")
        self.assertEqual(events.classify_pane("claude-code", "✻ Churning… (esc to interrupt)"), "busy")
        self.assertEqual(events.classify_pane("claude-code", "Churning… (3m 45s · ↓ 9.6k tokens"), "busy")
        self.assertEqual(events.classify_pane("claude-code", "Done.\n❯ "), "idle")

    def test_a_footer_cut_short_by_a_narrow_window_still_reads_as_busy(self):
        self.assertEqual(events.classify_pane("claude-code", "✻ Working · es…"), "busy")

    def test_an_unknown_tui_or_an_empty_pane_is_unclassified(self):
        self.assertIsNone(events.classify_pane("claude-code", "\n\n"))
        self.assertIsNone(events.classify_pane("mystery", "esc to interrupt"))

    def test_background_shells_are_counted_from_the_footer(self):
        self.assertEqual(events.pane_background("❯ \n· 3 shells · ? for shortcuts"), 3)
        self.assertEqual(events.pane_background("1 shell still running"), 1)
        self.assertEqual(events.pane_background("❯ "), 0)

    def test_pane_body_is_this_turns_reply_without_the_input_box_or_banners(self):
        rule = "─" * 30
        pane = "\n".join(["❯ old question", "old answer", "❯ fix the bug",
                          "Fixed it.", "Tests pass.", "✻ Worked for 3s", "",
                          rule, "❯ ", rule, "  ? for shortcuts"])
        self.assertEqual(events.pane_body(pane), "Fixed it.\nTests pass.")

    def test_session_key_names_container_sessions_by_container(self):
        self.assertEqual(events.session_key({"type": "local", "session": "web"}), "web")
        self.assertEqual(events.session_key({"type": "docker", "container": "box",
                                             "session": "default"}), "box/default")


class FinalMessages(unittest.TestCase):
    def jsonl(self, records, pad=None):
        path = scratch_dir() / "t.jsonl"
        with open(path, "w") as f:
            for r in records:
                f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
            for r in pad or []:
                f.write(json.dumps(r) + "\n")
        return str(path)

    def test_claudes_newest_assistant_text_is_its_final_message(self):
        path = self.jsonl([
            {"type": "assistant", "message": {"content": "first"}},
            {"type": "user", "message": {"content": "more"}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "All done."}, {"type": "tool_use", "name": "x"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "y"}]}},
            "{not json",
        ])
        self.assertEqual(events.claude_final_message(path), "All done.")

    def test_a_reply_further_back_than_the_first_tail_is_still_found(self):
        tool_only = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"blob": "x" * 2000}}]}}
        path = self.jsonl([{"type": "assistant", "message": {"content": "The real answer."}}],
                          pad=[tool_only] * 200)   # ~400 KB of tool calls after it
        self.assertGreater(os.path.getsize(path), 256 * 1024)
        self.assertEqual(events.claude_final_message(path), "The real answer.")

    def test_a_missing_transcript_has_no_message(self):
        self.assertEqual(events.claude_final_message(str(scratch_dir() / "nope.jsonl")), "")

    def test_antigravity_content_is_json_encoded(self):
        path = self.jsonl([
            {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": json.dumps("Built it.")},
            {"source": "USER", "type": "PLANNER_RESPONSE", "content": json.dumps("thanks")},
        ])
        self.assertEqual(events.agy_final_message(path), "Built it.")

    def test_copilot_reply_is_read_from_any_of_its_shapes(self):
        path = self.jsonl([
            {"type": "assistant.message", "data": {"content": "From data."}},
            {"role": "assistant", "content": [{"text": "From a list."}]},
            {"type": "user.message", "data": {"content": "ignored"}},
        ])
        self.assertEqual(events.copilot_final_message(path), "From a list.")


# ── events: the store ────────────────────────────────────────────────────────

class Store(unittest.TestCase):
    def setUp(self):
        self.db = events.connect(scratch_dir() / "events.db")

    def tearDown(self):
        self.db.close()

    def rec(self, event, source="hook", project="proj", instance="claude", **kw):
        return events.record(self.db, event=event, source=source, project=project,
                             instance=instance, agent_type="claude-code", **kw)

    def inst(self):
        return self.db.execute("SELECT * FROM instances WHERE project='proj'").fetchone()

    def test_a_turn_moves_the_instance_through_its_states(self):
        r = self.rec("prompt", ts=100.0)
        self.assertEqual((r["state"], r["changed"]), ("working", True))
        r = self.rec("stop", message="Done. Shall I deploy?", ts=110.0)
        self.assertEqual(r["state"], "needs_input")
        r = self.rec("dismiss", source="app", ts=120.0)
        self.assertEqual((r["state"], r["changed"]), ("idle", True))
        r = self.rec("dismiss", source="app", ts=130.0)
        self.assertEqual((r["state"], r["changed"]), ("idle", False))
        row = self.inst()
        self.assertEqual((row["state"], row["since"], row["updated"]), ("idle", 120.0, 130.0))
        self.assertEqual(row["last_hook"], 110.0)

    def test_an_event_that_changes_nothing_keeps_since_and_the_setting_event(self):
        first = self.rec("prompt", ts=100.0)
        self.rec("busy", source="pane", ts=105.0)
        row = self.inst()
        self.assertEqual((row["state"], row["since"], row["last_event"]), ("working", 100.0, first["id"]))

    def test_the_first_event_with_no_state_of_its_own_starts_idle(self):
        r = self.rec("notify", detail={"kind": "something"})
        self.assertEqual(r["state"], "idle")

    def test_only_the_tail_of_a_pane_is_kept(self):
        pane = "\n".join(f"line {i}" for i in range(200))
        r = self.rec("prompt", pane=pane)
        kept = self.db.execute("SELECT pane FROM events WHERE id=?", (r["id"],)).fetchone()["pane"]
        self.assertEqual(kept.splitlines(), [f"line {i}" for i in range(200 - events.PANE_KEEP, 200)])

    def test_background_task_badges_outlive_later_events(self):
        self.rec("prompt", detail={"background_tasks": 2})
        self.rec("stop", message="ok")
        self.assertEqual(json.loads(self.inst()["detail"]), {"background_tasks": 2})

    def test_mapping_a_session_fills_in_events_a_hook_logged_before(self):
        r = self.rec("prompt", project=None, instance=None, session="web-claude")
        events.sync_sessions(self.db, [("web-claude", "web", "claude", "claude-code")])
        row = self.db.execute("SELECT project, instance FROM events WHERE id=?", (r["id"],)).fetchone()
        self.assertEqual(tuple(row), ("web", "claude"))
        self.assertEqual(events.resolve_session(self.db, "web-claude"), ("web", "claude", "claude-code"))
        self.assertEqual(events.resolve_session(self.db, "other"), (None, None, None))
        events.sync_sessions(self.db, [])
        self.assertEqual(events.resolve_session(self.db, "web-claude"), (None, None, None))

    def test_prune_drops_old_events_but_not_the_one_holding_a_state(self):
        old = time.time() - 40 * 86400
        stale = self.rec("prompt", ts=old)
        holding = self.rec("stop", message="done", ts=old + 1)
        events.save_summary(self.db, stale["id"], "granite", summary="x")
        events.prune(self.db)
        ids = [r["id"] for r in self.db.execute("SELECT id FROM events")]
        self.assertEqual(ids, [holding["id"]])
        self.assertIsNone(events.summary_for(self.db, stale["id"]))

    def test_sonnets_summary_wins_over_the_local_one_unless_it_failed(self):
        eid = self.rec("stop", message="done")["id"]
        self.assertIsNone(events.summary_for(self.db, eid))
        events.save_summary(self.db, eid, "granite", model="g", summary="local", status="completed")
        events.save_summary(self.db, eid, "sonnet", error="timeout")
        self.assertEqual(events.summary_for(self.db, eid)["text"], "local")
        events.save_summary(self.db, eid, "sonnet", model="s", summary="better")
        got = events.summary_for(self.db, eid)
        self.assertEqual((got["text"], got["source"]), ("better", "sonnet"))

    def test_a_pane_detected_turn_is_reread_from_its_pane(self):
        r = self.rec("settled", source="pane", message="stale cut",
                     pane="❯ do it\nDid it.\n✻ Worked for 2s")
        row = self.db.execute("SELECT * FROM events WHERE id=?", (r["id"],)).fetchone()
        self.assertEqual(events.turn_text(row), "Did it.")
        r = self.rec("stop", message="From the hook.")
        row = self.db.execute("SELECT * FROM events WHERE id=?", (r["id"],)).fetchone()
        self.assertEqual(events.turn_text(row), "From the hook.")


# ── status / summarize helpers ───────────────────────────────────────────────

class Targets(unittest.TestCase):
    def script(self, text):
        d = scratch_dir()
        (d / "start.sh").write_text(text)
        return str(d)

    def test_a_local_session_is_read_from_the_launch_script(self):
        d = self.script('#!/bin/sh\nSESSION="web"\ntmux new -s "$SESSION"\n')
        self.assertEqual(status.parse_target(d, "./start.sh --flag"),
                         {"type": "local", "session": "web"})

    def test_a_container_session_needs_its_default_session(self):
        d = self.script('CONTAINER="box"\nDEFAULT_SESSION="main"\n')
        self.assertEqual(status.parse_target(d, "start.sh"),
                         {"type": "docker", "container": "box", "session": "main"})
        self.assertIsNone(status.parse_target(self.script('CONTAINER="box"\n'), "start.sh"))

    def test_no_script_or_no_session_is_no_target(self):
        self.assertIsNone(status.parse_target(str(scratch_dir()), "missing.sh"))
        self.assertIsNone(status.parse_target(self.script("echo hi\n"), "start.sh"))
        self.assertIsNone(status.parse_target(str(scratch_dir()), ""))

    def test_tmux_targets_match_a_session_name_exactly(self):
        self.assertEqual(status.tmux_session("bike"), "=bike")
        self.assertEqual(status.tmux_session("=bike"), "=bike")
        self.assertEqual(status.tmux_pane("bike"), "=bike:")

    def test_a_container_session_is_reached_through_docker_exec(self):
        self.assertEqual(status.tmux_argv({"type": "docker", "container": "box", "session": "s"},
                                          ["send-keys"], stdin=True),
                         ["docker", "exec", "-i", "box", "tmux", "send-keys"])
        self.assertEqual(status.tmux_argv({"type": "local", "session": "s"}, ["ls"]), ["tmux", "ls"])

    def test_agent_names_normalise_to_the_launcher_id(self):
        aid = status.DEFAULT_AGENT
        self.assertEqual(status.agent_id(None), aid)
        self.assertEqual(status.agent_id(f"  {aid.upper()} "), aid)
        self.assertEqual(status.agent_id(status.AGENTS[aid]["type"]), aid)
        with self.assertRaises(ValueError):
            status.agent_id("no-such-agent")

    def test_project_web_describes_main_extra_and_link(self):
        web = status.project_web({
            "web_port": 8080, "web_host": "box.lan", "web_path": "/app",
            "web_extra": [{"name": "api", "port": 9090, "path": "/v1"}, {"name": "dead"}],
            "web_start": "./serve.sh", "web_scheme": "https"})
        self.assertEqual(web["main"], {"name": None, "host": "box.lan", "port": 8080,
                                       "scheme": "https", "path": "/"})
        self.assertEqual([(x["name"], x["host"], x["port"], x["path"]) for x in web["extra"]],
                         [("api", "box.lan", 9090, "/v1")])
        self.assertEqual((web["link"]["path"], web["link"]["url"], web["start"]),
                         ("/app", None, "./serve.sh"))
        self.assertIsNone(status.project_web({"web_host": "x"}))

    def test_a_waiting_cards_reason_comes_from_what_put_it_there(self):
        self.assertEqual(status.state_reason({"event": "asking", "detail": None}),
                         "A menu or prompt is open in the terminal")
        self.assertEqual(status.state_reason({"event": "notify", "detail": json.dumps(
            {"kind": "permission_prompt", "text": "Allow Bash?"})}), "Allow Bash?")
        self.assertIsNone(status.state_reason({"event": "notify", "detail": json.dumps(
            {"kind": "idle_prompt", "text": "waiting"})}))
        self.assertIsNone(status.state_reason(None))


class Summaries(unittest.TestCase):
    def test_clean_pane_keeps_only_what_the_agent_printed(self):
        pane = "\x1b[1mBuilt\x1b[0m the thing\n" + "─" * 40 + "\n❯ \n? for shortcuts\n✻ Baked for 3s\nok"
        self.assertEqual(summarize.clean_pane(pane), "Built the thing\nok")

    def test_long_messages_keep_their_opening_and_their_end(self):
        msg = "START " + "x" * 5000 + " END?"
        clipped = summarize.clip_message(msg, head=10, tail=10)
        self.assertTrue(clipped.startswith("START"))
        self.assertTrue(clipped.endswith("END?"))
        self.assertIn("[…]", clipped)
        self.assertEqual(summarize.clip_message("  short  "), "short")

    def test_the_prompt_carries_the_last_paragraph_and_the_terminal_end(self):
        p = summarize.build_prompt("web", "Claude Code", "Did a.\n\nShall I push?", "out\n❯ ")
        self.assertIn("Project: web", p)
        self.assertIn("<last_paragraph>\nShall I push?\n</last_paragraph>", p)
        self.assertIn("<terminal>\nout\n</terminal>", p)


# ── server: prompts, tools, state ────────────────────────────────────────────

PROJECT = {"name": "web", "instances": []}


class Advisor(unittest.TestCase):
    def test_status_mode_appends_the_users_extra_ask(self):
        self.assertTrue(server.build_instruction("status", "").startswith("Give me a status update"))
        self.assertTrue(server.build_instruction("status", "and the tests?").endswith(
            "\n\nAlso: and the tests?"))

    def test_a_summary_turn_quotes_the_prompt_and_says_whether_it_is_mid_flight(self):
        done = server.build_instruction("summary", "fix it")
        live = server.build_instruction("summary_live", "fix it")
        self.assertIn("<submitted_prompt>\nfix it\n</submitted_prompt>", done)
        self.assertIn("stopped and gone quiet", done)
        self.assertIn("STILL WORKING", live)

    def test_a_chat_turn_is_the_message_itself(self):
        self.assertEqual(server.build_instruction("chat", "hello"), "hello")

    def test_each_kind_of_turn_gets_its_model(self):
        cfg = {**server.CFG, "model": "big", "status_model": "fast", "summary_model": "",
               "voice_model": "quick"}
        with mock.patch.object(server, "CFG", cfg):
            self.assertEqual(server.pick_model("status", True), "fast")
            self.assertEqual(server.pick_model("summary_live", False), "big")
            self.assertEqual(server.pick_model("chat", True), "quick")
            self.assertEqual(server.pick_model("chat", False), "big")

    def test_the_operator_gets_only_its_own_tools(self):
        tools, prompt = server.advisor_toolset("operator")
        self.assertEqual(tools, server.MCP_OPERATOR_TOOLS)
        self.assertNotIn("send_prompt", tools)
        self.assertIs(prompt, server.OPERATOR_TOOLS_PROMPT)

    def test_a_project_turn_gets_the_send_tools_with_their_prompt_or_neither(self):
        with mock.patch.dict(server.CFG, {"send_tool": True, "operator": True}):
            tools, prompt = server.advisor_toolset("project")
            self.assertEqual(tools, server.MCP_SEND_TOOLS + [server.MCP_BACK_TOOL])
            self.assertIn("send_prompt", prompt)
            self.assertIn(server.DISPATCH_SKILL, prompt)
        with mock.patch.dict(server.CFG, {"send_tool": False, "operator": False}):
            self.assertEqual(server.advisor_toolset("project"), ([], ""))

    def test_mcp_config_tells_the_tool_server_what_and_where(self):
        with mock.patch.object(server, "HTTP_PORT", 8000):
            cfg = json.loads(server.mcp_config(PROJECT, "project", ["send_prompt", "read_terminal"],
                                               agent="agy"))
        (name, spec), = cfg["mcpServers"].items()
        self.assertEqual(name, server.MCP_SERVER_NAME)
        self.assertEqual(spec["command"], sys.executable)
        self.assertEqual(spec["args"], [str(server.MCP_PATH)])
        self.assertEqual(spec["env"], {"VP_PROJECT": "web", "VP_BASE_URL": "http://127.0.0.1:8000",
                                       "VP_SCOPE": "project", "VP_TOOLS": "send_prompt,read_terminal",
                                       "VP_AGENT": "agy"})
        self.assertEqual(server.mcp_names(["a"]), ["mcp__voice_prompt__a"])

    def test_the_tool_server_calls_back_on_loopback_preferring_plain_http(self):
        with mock.patch.object(server, "HTTP_PORT", 8000), \
                mock.patch.object(server, "HTTPS_PORT", 8443):
            self.assertEqual(server.loopback_url(), "http://127.0.0.1:8000")
        with mock.patch.object(server, "HTTP_PORT", 0), \
                mock.patch.object(server, "HTTPS_PORT", 8443):
            self.assertEqual(server.loopback_url(), "https://127.0.0.1:8443")


class Conversations(unittest.TestCase):
    def setUp(self):
        self.path = scratch_dir() / "state.json"

    def test_history_survives_a_restart_and_is_capped(self):
        with mock.patch.dict(server.CFG, {"history_limit": 3}):
            s = server.State(self.path)
            for i in range(5):
                s.append("web", "user", f"m{i}", mode="chat", agent="claude")
            s.set_sid("web", "sid-1")
        again = server.State(self.path)
        self.assertEqual([m["text"] for m in again.history("web")], ["m2", "m3", "m4"])
        self.assertEqual(again.history("web")[0]["agent"], "claude")
        self.assertEqual(again.get_sid("web"), "sid-1")
        again.reset("web")
        self.assertEqual((again.history("web"), again.get_sid("web")), ([], None))

    def test_speech_rate_defaults_to_fast_and_ignores_junk(self):
        s = server.State(self.path)
        self.assertEqual(s.tts_rate(), "fast")
        s.set_tts_rate("slow")
        self.assertEqual(server.State(self.path).tts_rate(), "slow")
        s.set_tts_rate("ludicrous")
        self.assertEqual(s.tts_rate(), "fast")

    def test_selection_is_empty_until_chosen(self):
        s = server.State(self.path)
        self.assertEqual(s.selected(), [])
        s.set_selected(("a", "b"))
        self.assertEqual(server.State(self.path).selected(), ["a", "b"])

    def test_a_corrupt_state_file_starts_fresh(self):
        self.path.write_text("{nope")
        self.assertEqual(server.State(self.path).history("web"), [])


class DeskFeed(unittest.TestCase):
    def test_the_window_reads_only_what_is_newer_than_it_saw(self):
        start = server.desk_payload(0)["seq"]
        server.note_desk({"kind": "operator"})
        server.desk_nav({"view": "agent", "project": "web", "agent": None})
        server.desk_nav(None)
        got = server.desk_payload(start)
        self.assertEqual(got["seq"], start + 2)
        self.assertEqual([e["kind"] for e in got["events"]], ["operator", "agent"])
        self.assertEqual(got["events"][1]["agent"], server.DEFAULT_AGENT)
        self.assertEqual(server.desk_payload(start + 2)["events"], [])

    def test_only_the_newest_events_are_kept(self):
        for i in range(server.DESK_KEEP + 10):
            server.note_desk({"kind": "x", "i": i})
        got = server.desk_payload(0)["events"]
        self.assertEqual(len(got), server.DESK_KEEP)
        self.assertEqual(got[-1]["i"], server.DESK_KEEP + 9)

    def test_advisor_sends_and_navigation_drain_once(self):
        server.note_advisor_send("web", {"text": "a"})
        server.note_advisor_send("web", {"text": "b"})
        self.assertEqual([s["text"] for s in server.drain_advisor_sends("web")], ["a", "b"])
        self.assertEqual(server.drain_advisor_sends("web"), [])
        server.note_nav("web", {"view": "projects"})
        self.assertEqual(server.drain_nav("web"), {"view": "projects"})
        self.assertIsNone(server.drain_nav("web"))


# ── server: where it listens ─────────────────────────────────────────────────

class BindPolicy(unittest.TestCase):
    TS = "100.101.102.103"

    def addrs(self, **cfg):
        with mock.patch.object(server.LISTEN, "interface_ip", lambda name: self.TS):
            return server.bind_addresses(cfg)

    def test_the_default_is_this_machine_only(self):
        self.assertEqual(self.addrs(), ["127.0.0.1"])

    def test_the_tailnet_needs_no_token(self):
        self.assertEqual(self.addrs(bind="tailscale"), ["127.0.0.1", self.TS])

    def test_every_interface_without_a_token_refuses_to_start(self):
        with self.assertRaises(SystemExit) as e:
            self.addrs(bind="any")
        self.assertIn("token", str(e.exception))

    def test_a_token_or_an_explicit_opt_out_allows_it(self):
        self.assertEqual(self.addrs(bind="any", token="s3cret"), ["0.0.0.0"])
        self.assertEqual(self.addrs(bind="192.168.1.20", allow_no_token=True), ["192.168.1.20"])

    def test_the_mcp_callback_stays_on_loopback(self):
        with mock.patch.object(server, "BIND_ADDRS", ["127.0.0.1", self.TS]), \
             mock.patch.object(server, "HTTP_PORT", 4321):
            self.assertEqual(server.loopback_url(), "http://127.0.0.1:4321")


# ── server: HTTP ─────────────────────────────────────────────────────────────

class QuietHandler(server.Handler):
    def log_message(self, *a):
        pass


class HTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)   # an ephemeral port
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def call(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def test_no_token_configured_lets_everyone_in(self):
        with mock.patch.dict(server.CFG, {"token": ""}):
            code, _, body = self.call("GET", "/api/desk?after=0")
        self.assertEqual(code, 200)
        self.assertIn("seq", json.loads(body))

    def test_with_a_token_the_header_or_the_query_must_carry_it(self):
        with mock.patch.dict(server.CFG, {"token": "s3cret"}):
            self.assertEqual(self.call("GET", "/api/desk")[0], 401)
            self.assertEqual(self.call("GET", "/api/desk", {"X-VP-Token": "wrong"})[0], 401)
            self.assertEqual(self.call("GET", "/api/desk?token=wrong")[0], 401)
            self.assertEqual(self.call("GET", "/api/desk", {"X-VP-Token": "s3cret"})[0], 200)
            self.assertEqual(self.call("GET", "/api/desk?after=0&token=s3cret")[0], 200)
            self.assertEqual(self.call("POST", "/api/reset", body=b"{}")[0], 401)

    def test_a_wrong_header_is_not_rescued_by_a_right_query(self):
        with mock.patch.dict(server.CFG, {"token": "s3cret"}):
            self.assertEqual(self.call("GET", "/api/desk?token=s3cret", {"X-VP-Token": "wrong"})[0], 401)

    def test_a_non_ascii_token_attempt_is_refused_not_an_error(self):
        with mock.patch.dict(server.CFG, {"token": "s3cret"}):
            self.assertEqual(self.call("GET", "/api/desk?token=%C3%A9")[0], 401)

    def test_voice_routes_are_refused_while_the_feature_is_off(self):
        with mock.patch.dict(server.CFG, {"token": ""}), mock.patch.object(server, "VOICE_ON", False):
            code, _, body = self.call("POST", "/api/chat", body=b'{"project": "web"}')
        self.assertEqual(code, 403)
        self.assertIn("voice feature is off", json.loads(body)["error"])

    def test_plain_http_page_loads_from_the_lan_are_sent_to_https(self):
        with mock.patch.object(server, "HTTPS_READY", True), \
                mock.patch.object(server, "HTTPS_PORT", 8443):
            code, headers, _ = self.call("GET", "/?x=1", {"Host": "192.168.1.5:8000"})
            self.assertEqual((code, headers["Location"]), (307, "https://192.168.1.5:8443/?x=1"))
            code, headers, _ = self.call("POST", "/api/nope", {"Host": "[fd00::2]:8000"})
            self.assertEqual(code, 404)   # /api is never bounced
            code, headers, _ = self.call("GET", "/", {"Host": "[fd00::2]:8000"})
            self.assertEqual(headers["Location"], "https://[fd00::2]:8443/")

    def test_localhost_and_a_tls_terminating_proxy_stay_on_http(self):
        with mock.patch.object(server, "HTTPS_READY", True), \
                mock.patch.object(server, "HTTPS_PORT", 8443):
            for host in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000"):
                self.assertEqual(self.call("GET", "/vendor/none", {"Host": host})[0], 404, host)
            self.assertEqual(self.call("GET", "/vendor/none", {
                "Host": "box.ts.net", "X-Forwarded-Proto": "https"})[0], 404)


if __name__ == "__main__":
    unittest.main()

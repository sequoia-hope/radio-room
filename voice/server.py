#!/usr/bin/env python3
"""voice_prompt — phone-friendly web UI for talking to a Sonnet advisor about
the coding agents (Claude Code / Antigravity / opencode) running in per-project
tmux sessions.

Projects come from the registry (core/registry.py). A project can run more
than one agent — Claude Code in its base session, Antigravity (agy) or Copilot
beside it — and each of those instances has its own tmux target (local
session, or a session inside a docker container). Targets are resolved by the
registry, so the naming rule lives in one place; overrides for the default
instance can be set in the config file's [voice.targets] tables.

Settings are the [voice] and [status] tables of Radio Room's config file
(core/config.py). Ports are the registry's: the HTTPS listener is the voice
project's web_port, and the plain-HTTP redirector its web_extra named "http".

The advisor is a separate Sonnet instance run via `claude -p` (headless), so it
uses your existing Claude Code auth. It gets a fresh capture of the project's
tmux pane with every message, read-only access to the project directory, and —
via the bundled MCP server (mcp_server.py) — a tool for submitting prompts to
the agent, so work can be dispatched hands-free.

Stdlib only — requires Python 3.11+ (tomllib), tmux, and the claude CLI.
"""

import hmac
import importlib.util
import json
import os
import re
import shlex
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import xml.etree.ElementTree as ElementTree
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import events
import live
import status
import summarize
import usage
from status import (AGENTS, DEFAULT_AGENT, agent_id, alive_checker, capture_pane,
                    dismiss_instance, set_pin, event_payload, get_instance, get_project,
                    instances_payload, load_projects, send_to_project, session_alive,
                    status_db, target_desc, tmux_argv, tmux_pane)

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "core"))
import config as radio_config  # noqa: E402
import registry as REGISTRY  # noqa: E402
import cpu as CPU  # noqa: E402
import listen as LISTEN  # noqa: E402
import pastes as PASTES  # noqa: E402
STATE_PATH = ROOT / "state.json"
INDEX_PATH = ROOT / "static" / "index.html"
VENDOR = ROOT.parent / "terminal" / "static" / "vendor"   # xterm.js, shared with terminal/
MCP_PATH = ROOT / "mcp_server.py"
PLUGIN_PATH = ROOT / "plugin"      # carries the dispatch skill (see plugin/)

DEFAULT_CONFIG = {
    "bind": "local",             # see core/listen.py: local, tailscale, any, or an address
    "allow_no_token": False,     # beyond loopback and Tailscale, listen without a token
    "pane_lines": 200,
    "history_limit": 200,
    "claude_bin": "claude",
    "model": "sonnet",           # typed discussion / brainstorm
    "status_model": "haiku",     # one-tap status summaries (speed matters)
    "voice_model": "haiku",      # hands-free conversation turns (latency matters)
    "allowed_tools": "Read,Glob,Grep",
    "send_tool": True,           # let the advisor submit prompts itself (MCP)
    "chat_timeout": 600,
    "summarize": True,           # summarize the agent's reply to each prompt
    "summary_model": "haiku",
    "summary_poll": 3,           # seconds between pane checks while watching
    "summary_settle": 9,         # seconds of an unchanged pane = agent stopped
    "summary_timeout": 1800,     # give up watching after this long
    "operator": True,            # multi-project mode (the projects view)
    "operator_pane_lines": 60,   # per project — several panes ride on one turn
    "operator_max_panes": 8,     # cap the terminals attached to one turn
    "music": True,               # background music player in the web UI
    "music_feed": "https://musicforprogramming.net/rss.xml",
    "music_ttl": 21600,          # seconds to cache the episode list
    # Local speech servers, for browsers with no Web Speech API of their own
    # (Firefox has none) and for keeping voice off the cloud. Empty = off, and
    # the browser's built-in speech is used instead. Both are plain HTTP
    # endpoints this server proxies to; see the README for what to run.
    "stt_url": "",               # OpenAI-style /v1/audio/transcriptions
    "stt_model": "Systran/faster-whisper-medium",
    "stt_language": "en",        # "" to let the model guess
    "stt_timeout": 120,
    "stt_max_bytes": 25_000_000,
    "tts_url": "",               # Kokoro-FastAPI /v1/audio/speech
    "tts_voices_url": "",        # defaults to tts_url with speech -> voices
    "tts_model": "kokoro",
    "tts_voice": "af_heart",     # the page's default; each browser remembers its own
    "tts_format": "mp3",
    "tts_timeout": 120,
    "token": "",
    # The browser terminal the agent cards open (its ?attach= link): a
    # registry project, so its address is never written down twice.
    "terminal_project": "",
}


def load_config() -> dict:
    """The config file's [voice] table over the defaults. Agent status has its
    own, [status] (status.py)."""
    return radio_config.section("voice", DEFAULT_CONFIG)


CFG = load_config()
# The page is served either way; the "voice" feature is the advisor, speech,
# music and sending prompts — everything that needs the claude CLI or a mic.
VOICE_ON = radio_config.feature("voice")
VOICE_GET = {"/api/history", "/api/updates", "/api/overview", "/api/music", "/api/voices"}
VOICE_POST = {"/api/stt", "/api/chat", "/api/send", "/api/tts", "/api/tts/rate", "/api/selection",
              "/api/nav", "/api/event/sonnet", "/api/reset"}

SYSTEM_PROMPT = """\
You are a hands-free advisor for a developer who is away from their desk,
talking to you from a phone with voice dictation. Each of their projects has a
coding agent working in a tmux session — Claude Code, Antigravity (Google's
`agy` CLI), or opencode, possibly running Opus, Gemini or another model — and a
project can run more than one of them side by side. You are NOT any of those
agents — you are a second pair of eyes. Each message names which agent the
user is focused on and carries a fresh snapshot of its terminal,
and you have read-only access (Read/Glob/Grep) to the project's working
directory if you need to check files. You cannot run commands or edit files
yourself.

Your jobs:
- Status: explain what the agent has been doing, what state it's in (working /
  waiting on the user / done / stuck / erroring), and anything needing a
  decision. If the agent asked a question, quote it and suggest an answer.
- Brainstorm: think through options and tradeoffs conversationally.
- Draft prompts: when the user wants to send the agent a new instruction, help
  write it. Put the final ready-to-send prompt in a single fenced code block so
  the UI can pick it up.

Style: read on a phone, often while multitasking. Be conversational and
concise — short paragraphs, minimal headers and tables, no big code dumps
unless asked. Lead with the answer.
"""

# Appended when the send tool (mcp_server.py) is wired in. The craft — wording
# the prompt, confirming, handling failures — lives in the dispatch skill; what
# has to hold even if the skill is never opened stays here.
SEND_TOOL_PROMPT = """
Dispatching work: you also have send_prompt, which types a prompt into the
agent's terminal and presses Enter — the same as the user typing it. They are
often hands-free, so this is how a spoken instruction actually reaches the
agent. read_terminal re-captures that terminal on demand.

Use send_prompt only when the user asks you to send/tell/ask the agent
something ("send it", "tell it to ...", "go ahead", "yes, send that"). Never
send on your own initiative, and never send mid-brainstorm — brainstorming is
discussion. Once they've asked, send in that same turn: there is no
confirmation dialog on this path, and they can't conveniently type one.

Two more tools drive the agent's session: clear_agent_context submits /clear to
it, and set_agent_model submits /model <name>. Both act on the AGENT. Every
tool acts on the agent the user is focused on; pass `agent` only when they
clearly mean the project's other one. Your own
conversation and your own model aren't yours to change — the user handles those
on the page — so if they ask you to clear or reswitch *yourself*, say that's a
tap on their end rather than reaching for a tool.

Read the {skill} skill before your first send in a conversation — it covers how
to word the prompt so the agent (which can't see this conversation) can act on
it, what to say afterwards, what to do when a send fails, and how the two
scopes differ.
"""

# The operator is a conversation like any project's, so it lives in the same
# store under a reserved key. Launcher project names come from directory names,
# which never start with "@".
OPERATOR_KEY = "@operator"

OPERATOR_PROMPT = """\
You are the operator for a developer who is away from their desk, talking to
you from a phone by voice. They have several projects, each with one or more
coding agents (Claude Code, Antigravity — Google's `agy` CLI — or opencode)
working in their own tmux sessions. You run the machine around those agents —
which sessions and web apps are up, what the user is looking at — but you never
prompt an agent yourself. When a project runs two agents, say which one you
mean.

Some projects are "selected": that is the working set, and every message you
get carries a fresh snapshot of each selected project's terminal. read_terminal
fetches any project's terminal on demand, selected or not.

Your jobs:
- Round-ups. Across the selected projects, say who is working, who is waiting
  on the user, who has finished, and who is stuck or erroring. Lead with
  whatever needs them. Always name the project you're talking about — they
  can't see which pane is which, and "it" is ambiguous when there are five.
- One project at a time. Answer questions about any single project's state.
- Curate the working set with select_projects, when they say which projects to
  watch ("watch rover and the website", "drop everything but rover").
- Hand off with open_project, which switches their screen into one project.
  There a per-project advisor takes over and can actually dispatch work. Use it
  when they say to go into / open / switch to / work on something. You cannot
  send prompts to any agent yourself — diving in is how that happens, so offer
  it when what they want is for an agent to do something.
- Start and stop things: an agent's session (start_agent / stop_agent) and a
  project's web app (start_web / stop_web). "Spin up garmin and take me
  there" is start_agent then open_project, in one turn.

Style: read on a phone, often hands-free and while multitasking. Short and
conversational, no markdown, no tables, no code dumps. Lead with the answer.
When nothing has moved since last time, say so in a line rather than restating
everything.
"""

# Appended for the operator when its tools are wired in.
OPERATOR_TOOLS_PROMPT = """
Tools: select_projects sets the working set, open_project hands the screen to
one project, read_terminal captures any project's terminal right now.
start_agent / stop_agent start or kill a project's agent session, start_web /
stop_web bring its web app up or down. The roster in each message says what is
running. cpu_queue reads the machine's CPU reservation queue (heavy builds and
test suites wait there for cores); cpu_control stops, cancels, reorders or
resizes its jobs, pauses it, or changes the cores kept free. set_speech_speed
sets how fast your replies are spoken (slow, medium or fast) when they ask you
to talk slower or faster; it takes effect from this reply on.

Stopping kills whatever is in the session, mid-turn or not, so stop only when
they asked for that project by name. If the agent is working when they ask,
say so and stop it only once they confirm.

Act in the same turn they ask — they are hands-free and can't tap a
confirmation. Say what you did in your reply, briefly; the screen changes under
them, so a silent open_project is disorienting.
"""

# Appended for a project advisor when the projects view exists to go back to.
BACK_TO_PROJECTS_PROMPT = """
You also have open_projects_view, which takes their screen back to the
multi-project overview and its operator. Use it when they say to go back, show
all the projects, or leave this one. This conversation stays exactly where it
is, so coming back later picks up where they left off.
"""

MCP_SERVER_NAME = "voice_prompt"
MCP_SEND_TOOLS = ["send_prompt", "read_terminal", "clear_agent_context",
                  "set_agent_model"]
MCP_BACK_TOOL = "open_projects_view"
MCP_OPERATOR_TOOLS = ["read_terminal", "select_projects", "open_project",
                      "start_agent", "stop_agent", "start_web", "stop_web",
                      "cpu_queue", "cpu_control", "set_speech_speed"]


def mcp_names(tools: list[str]) -> list[str]:
    return [f"mcp__{MCP_SERVER_NAME}__{t}" for t in tools]
DISPATCH_SKILL = "voice-prompt:dispatch-to-agent"


def instance_view(name: str, inst: dict, alive) -> dict:
    """An instance as the page sees it."""
    return {"id": inst["id"], "type": inst["type"], "label": inst["label"],
            "target": inst["target"], "alive": alive(inst["target"]),
            "watching": watching(name, inst["id"])}


def overview_instances(project: dict, alive) -> list[dict]:
    """What a roster shows: the default instance always (it's where a project
    is launched by default, running or not), the others only while running —
    an Antigravity slot nobody has ever started is not worth a chip."""
    return [v for v in (instance_view(project["name"], i, alive)
                        for i in project["instances"])
            if v["id"] == DEFAULT_AGENT or v["alive"]]



# ── Advisor (Sonnet via `claude -p`) ─────────────────────────────────────────

class AdvisorError(RuntimeError):
    pass


VOICE_NOTE = (
    "\n\n(Voice mode: my phone reads your reply aloud with text-to-speech. "
    "Answer in plain conversational prose — no markdown, headers, bullet "
    "lists, tables, or code blocks unless I explicitly ask for a draft "
    "prompt. Keep it to a few sentences unless I ask for depth.)"
)


def build_instruction(mode: str, message: str) -> str:
    if mode == "status":
        base = ("Give me a status update: what has the agent been doing, what "
                "state is it in now (working / waiting on me / done / stuck), "
                "and is there anything that needs my attention or a decision? "
                "Answer from the terminal snapshot; only read files if truly "
                "necessary.")
        return base + (f"\n\nAlso: {message}" if message else "")
    if mode == "brainstorm":
        base = message or "Given where things stand, brainstorm sensible next steps."
        return "Let's brainstorm — discussion only, keep it conversational:\n" + base
    if mode.startswith("summary"):
        # Raised by the reply watcher, not the user. `message` is the prompt
        # that was submitted to the agent.
        state = ("it has now stopped and gone quiet" if mode == "summary" else
                 "it is STILL WORKING — this is a look mid-flight, not a "
                 "finished result")
        return (
            f"This prompt was submitted to the agent, and {state}:\n"
            f"<submitted_prompt>\n{message}\n</submitted_prompt>\n\n"
            "Tell me what the agent did in response and where things stand, in "
            "one to three short sentences of plain conversational prose — this "
            "gets read aloud, so no markdown, bullets, headers or code blocks. "
            "Lead with the outcome. If it's asking a question or waiting on a "
            "decision, say so and quote the question. If it errored or stopped "
            "early, say what failed. Don't send anything to the agent this "
            "turn — I haven't asked for anything yet."
        )
    return message


def siblings_note(project: dict, inst: dict) -> str:
    """One line about the project's other agents, so the advisor knows the
    user may be talking about two different terminals in one conversation."""
    others = [i for i in project["instances"] if i["id"] != inst["id"]]
    alive = alive_checker()
    running = [f'{i["label"]} (agent="{i["id"]}", {target_desc(i["target"])})'
               for i in others if alive(i["target"])]
    if not running:
        return ""
    return ("\n\nThis project also has running: " + "; ".join(running) + ". "
            "That terminal isn't attached here; read_terminal with its agent "
            "id fetches it.")


def build_prompt(project: dict, inst: dict, mode: str, message: str,
                 pane: str | None, voice: bool = False) -> str:
    snapshot = pane if pane is not None else \
        "(no terminal output — the tmux session isn't running)"
    return (
        f'The user is focused on the "{project["name"]}" project\'s '
        f'{inst["label"]} agent (agent="{inst["id"]}"). Fresh snapshot of its '
        f'terminal ({target_desc(inst["target"])}) — snapshots in earlier '
        f"messages are stale:\n<terminal_snapshot>\n{snapshot}\n</terminal_snapshot>"
        + siblings_note(project, inst) + "\n\n"
        + build_instruction(mode, message)
        + (VOICE_NOTE if voice else "")
    )


def pick_model(mode: str, voice: bool) -> str:
    if mode == "status":
        return CFG.get("status_model") or CFG["model"]
    if mode.startswith("summary"):
        return CFG.get("summary_model") or CFG["model"]
    if voice:
        return CFG.get("voice_model") or CFG["model"]
    return CFG["model"]


def send_tool_enabled() -> bool:
    return bool(CFG.get("send_tool")) and MCP_PATH.exists()


def loopback_url() -> str:
    """URL the MCP subprocess uses to call back into this server."""
    host = next((a for a in BIND_ADDRS if LISTEN.is_loopback(a)), None)
    if host is None:
        host = "127.0.0.1" if BIND_ADDRS[0] in ("0.0.0.0", "::") else BIND_ADDRS[0]
    if HTTP_PORT:
        return f"http://{host}:{HTTP_PORT}"
    return f"https://{host}:{HTTPS_PORT}"


def mcp_config(project: dict, scope: str, tools: list[str],
               agent: str = DEFAULT_AGENT) -> str:
    """Inline --mcp-config for one advisor turn. VP_TOOLS keeps server.py the
    single source of truth for what exists: the MCP server publishes exactly
    the tools named here, so --allowedTools and tools/list can't drift.
    VP_AGENT is the instance the user is focused on — what a tool acts on when
    the advisor doesn't name one."""
    return json.dumps({"mcpServers": {MCP_SERVER_NAME: {
        "command": sys.executable,
        "args": [str(MCP_PATH)],
        "env": {"VP_PROJECT": project["name"], "VP_BASE_URL": loopback_url(),
                "VP_SCOPE": scope, "VP_TOOLS": ",".join(tools),
                "VP_AGENT": agent},
    }}})


def operator_enabled() -> bool:
    return bool(CFG.get("operator")) and MCP_PATH.exists()


def advisor_toolset(scope: str) -> tuple[list[str], str]:
    """Which MCP tools this turn gets, and the system-prompt text explaining
    them. The two are decided together so a prompt never describes a tool the
    turn wasn't given."""
    if scope == "operator":
        return list(MCP_OPERATOR_TOOLS), OPERATOR_TOOLS_PROMPT
    tools, extra = [], ""
    if send_tool_enabled():
        tools += MCP_SEND_TOOLS
        extra += SEND_TOOL_PROMPT.format(skill=DISPATCH_SKILL)
    if operator_enabled():
        tools.append(MCP_BACK_TOOL)
        extra += BACK_TO_PROJECTS_PROMPT
    return tools, extra


def claude_json(args: list[str], prompt: str, cwd: str, timeout: int) -> dict:
    """Run one headless `claude -p` turn; return the parsed JSON envelope."""
    # Don't leak this-server-was-launched-from-Claude-Code env into the child.
    env = {k: v for k, v in os.environ.items()
           if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE")}
    # The advisor is a Claude Code session too, so the status hooks would log
    # its turns as if an agent had worked. This tells vp_event.py to skip it.
    env["VP_STATUS_HOOK"] = "off"
    p = subprocess.run([CFG["claude_bin"], "-p", "--output-format", "json", *args],
                       input=prompt, text=True, capture_output=True,
                       cwd=cwd, env=env, timeout=timeout)
    if p.returncode != 0:
        raise AdvisorError((p.stderr or p.stdout).strip()[-2000:]
                           or f"claude exited {p.returncode}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        raise AdvisorError("unparseable claude output: " + p.stdout.strip()[-2000:])


def run_advisor(project: dict, prompt: str, resume_sid: str | None,
                model: str | None = None, scope: str = "project",
                agent: str = DEFAULT_AGENT):
    """Run one advisor turn. Returns (reply_text, session_id).

    scope="operator" runs the multi-project operator instead: no project
    directory to read (it spans several, and reads terminals through a tool
    rather than files), so it gets no filesystem tools at all."""
    operator = scope == "operator"
    tools = "" if operator else CFG["allowed_tools"]
    system = OPERATOR_PROMPT if operator else SYSTEM_PROMPT
    extra: list[str] = []
    mcp_tools, tool_prompt = advisor_toolset(scope)
    if mcp_tools:
        tools = ",".join(filter(None, [tools, *mcp_names(mcp_tools)]))
        system += tool_prompt
        # --strict-mcp-config: only our server, not the user's global ones.
        extra = ["--mcp-config", mcp_config(project, scope, mcp_tools, agent),
                 "--strict-mcp-config"]
        if PLUGIN_PATH.is_dir() and not operator:
            # Ships the dispatch skill. It has to ride along as a plugin: the
            # advisor's cwd is the *project's* directory, so a .claude/skills
            # here would never be found.
            extra += ["--plugin-dir", str(PLUGIN_PATH)]
            tools += f",Skill({DISPATCH_SKILL})"

    base = ["--model", model or CFG["model"],
            "--allowedTools", tools,
            *extra,
            "--append-system-prompt", system]

    def attempt(sid):
        cmd = base + (["--resume", sid] if sid else [])
        return claude_json(cmd, prompt, project["dir"], CFG["chat_timeout"])

    try:
        data = attempt(resume_sid)
    except AdvisorError:
        if not resume_sid:
            raise
        data = attempt(None)  # stored session may have expired — start fresh
    return data.get("result", "").strip(), data.get("session_id")


# ── Persistent state (advisor sessions + chat history) ───────────────────────

# The page maps these to a speech rate (browser) or speed (Kokoro).
TTS_RATES = ("slow", "medium", "fast")


class State:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            self.data = {"projects": {}}
        self.data.setdefault("projects", {})

    def _proj(self, name):
        return self.data["projects"].setdefault(name, {"sid": None, "messages": []})

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)

    def get_sid(self, name):
        with self.lock:
            return self._proj(name).get("sid")

    def set_sid(self, name, sid):
        with self.lock:
            self._proj(name)["sid"] = sid
            self._save()

    def append(self, name, role, text, mode=None, agent=None):
        with self.lock:
            msgs = self._proj(name)["messages"]
            msg = {"role": role, "text": text, "mode": mode, "ts": time.time()}
            if agent:
                msg["agent"] = agent
            msgs.append(msg)
            limit = CFG["history_limit"]
            if len(msgs) > limit:
                del msgs[:len(msgs) - limit]
            self._save()

    def history(self, name):
        with self.lock:
            return list(self._proj(name)["messages"])

    def reset(self, name):
        with self.lock:
            self.data["projects"][name] = {"sid": None, "messages": []}
            self._save()

    def selected(self):
        """Names the operator is watching. Empty until chosen — this machine has
        27 projects, so "watch everything" is not a sane default; it would hang
        every terminal on the planet off each turn."""
        with self.lock:
            got = self.data.get("selected")
            return list(got) if isinstance(got, list) else []

    def set_selected(self, names):
        with self.lock:
            self.data["selected"] = list(names)
            self._save()

    def tts_rate(self):
        """How fast replies are spoken: one of TTS_RATES. The page's speed
        buttons and the operator's set_speech_speed both write it."""
        with self.lock:
            got = self.data.get("tts_rate")
            return got if got in TTS_RATES else "fast"

    def set_tts_rate(self, name):
        with self.lock:
            self.data["tts_rate"] = name
            self._save()


STATE = State(STATE_PATH)

_chat_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_chat_locks_guard = threading.Lock()


def chat_lock(name: str) -> threading.Lock:
    with _chat_locks_guard:
        return _chat_locks[name]


# Prompts the advisor submitted through the MCP send tool. The tool calls back
# into /api/send while a chat turn is in flight; /api/chat drains this so the
# browser can show the "sent to project" bubble without a reload.
_advisor_sends: dict[str, list[dict]] = defaultdict(list)
_advisor_sends_guard = threading.Lock()


def note_advisor_send(name: str, sent: dict) -> None:
    with _advisor_sends_guard:
        _advisor_sends[name].append(sent)


def drain_advisor_sends(name: str) -> list[dict]:
    with _advisor_sends_guard:
        return _advisor_sends.pop(name, [])


# Where an advisor asked to send the user's screen. Same trick as the sends
# above: the tool calls back mid-turn, /api/chat drains it, and the page acts on
# it when the reply lands — so the view changes with the words that explain it.
_nav: dict[str, dict] = {}
_nav_guard = threading.Lock()


def note_nav(key: str, nav: dict) -> None:
    with _nav_guard:
        _nav[key] = nav


def drain_nav(key: str) -> dict | None:
    with _nav_guard:
        return _nav.pop(key, None)


# What the desktop window (desktop/) should put in front of the user: the
# project the screen moved to, a session just started, a web app just brought
# up. A numbered feed rather than a queue, since nothing here knows whether a
# window is even open; it reads what is newer than the last number it saw.
_desk: list[dict] = []
_desk_seq = 0
_desk_guard = threading.Lock()
DESK_KEEP = 50


def note_desk(event: dict) -> None:
    global _desk_seq
    with _desk_guard:
        _desk_seq += 1
        _desk.append({**event, "seq": _desk_seq})
        del _desk[:-DESK_KEEP]


def desk_payload(after: int) -> dict:
    with _desk_guard:
        return {"seq": _desk_seq, "events": [e for e in _desk if e["seq"] > after]}


def desk_nav(nav: dict | None) -> None:
    """A screen move, once its turn has ended, for the window to follow."""
    if not nav:
        return
    if nav["view"] == "projects":
        note_desk({"kind": "operator"})
    else:
        note_desk({"kind": "agent", "project": nav["project"],
                   "agent": nav.get("agent") or DEFAULT_AGENT})


def registry_payload() -> dict:
    """Every project as the Projects tab shows it — the registry's own status
    (agents, git, tickets, web) with each project's token usage."""
    usnap = usage.snapshot(status_db()) if status.CFG.get("usage") else {"by_project": {}}
    out = []
    for e in REGISTRY.status(REGISTRY.load()):
        out.append({k: e[k] for k in ("name", "desc", "port", "path", "host", "scheme",
                                      "url", "up", "managed", "extra", "agents", "git",
                                      "tickets", "jira", "pinned")}
                   | {"usage": usnap["by_project"].get(e["name"])})
    return {"projects": out}


def tickets_payload() -> dict:
    """Your tickets (the config's JQL) and the projects linked to each."""
    jira = REGISTRY.jira
    if not jira.enabled():
        raise KeyError("Jira is not configured")
    try:
        issues = jira.Jira().search()
    except jira.JiraError as e:
        raise UpstreamError(str(e))
    linked: dict[str, list[str]] = {}
    for p in REGISTRY.load()["projects"]:
        for k in REGISTRY.tickets(p):
            linked.setdefault(k, []).append(p["name"])
    return {"issues": issues, "linked": linked}


def ticket_payload(key: str) -> dict:
    jira = REGISTRY.jira
    if not jira.enabled():
        raise KeyError("Jira is not configured")
    try:
        return jira.Jira().issue(key)
    except jira.JiraError as e:
        raise UpstreamError(str(e))


def control(action: str, name: str, agent: str = DEFAULT_AGENT) -> str:
    """Start or stop a project's agent session or web app — the launcher's and
    the hub's own functions, so nothing here knows how either is done."""
    if REGISTRY is None:
        raise UpstreamError("the launcher's registry.py didn't load; can't "
                            "start or stop anything")
    p = REGISTRY.find(REGISTRY.load(), name)
    if p is None:
        raise KeyError(name)
    if action in ("start_agent", "stop_agent"):
        if agent not in AGENTS:
            raise ValueError(f"unknown agent {agent!r} — use one of {', '.join(AGENTS)}")
        if action == "stop_agent":
            ok, msg = REGISTRY.stop_agent(p, agent)
            if not ok:
                raise UpstreamError(msg or "not running")
            return f"stopped {name}'s {agent} session"
        try:
            REGISTRY.start_agent(p, agent)
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            raise UpstreamError(str(e))
        note_desk({"kind": "agent", "project": name, "agent": agent})
        return f"{name}'s {agent} session is running"
    if action in ("start_web", "stop_web"):
        ok, msg = (REGISTRY.start_web if action == "start_web" else REGISTRY.stop_web)(p)
        if not ok:
            raise UpstreamError(msg or "failed")
        if action == "start_web":
            note_desk({"kind": "web", "project": name})
        return msg or "done"
    raise ValueError(f"unknown action {action!r}")


def web_up(p: dict) -> bool | None:
    """Whether a project's web app answers; None when it has none."""
    web = p.get("web")
    if not web or REGISTRY is None:
        return None
    return REGISTRY.is_up(web["main"]["port"])


def selected_projects() -> list[dict]:
    keep = set(STATE.selected())
    return [p for p in load_projects() if p["name"] in keep]


def build_operator_prompt(mode: str, message: str, voice: bool = False) -> str:
    """One turn's worth of context for the operator: every selected project's
    terminal, plus the full roster so it can select or open by name."""
    watching_now = selected_projects()
    cap = max(1, int(CFG["operator_max_panes"]))
    alive = alive_checker()
    # The cap is on terminals, not projects: a project running two agents
    # attaches two. One that runs neither still gets a block saying so.
    blocks, omitted = [], []
    for p in watching_now:
        live = [i for i in p["instances"] if alive(i["target"])]
        if len(blocks) + max(1, len(live)) > cap:
            omitted.append(p)
            continue
        if not live:
            blocks.append(f'<project name="{p["name"]}">\n(no agent session is '
                          "running)\n</project>")
        for i in live:
            pane = capture_pane(i["target"], CFG["operator_pane_lines"])
            snapshot = pane if pane is not None else "(tmux session isn't running)"
            blocks.append(f'<project name="{p["name"]}" agent="{i["id"]}" '
                          f'type="{i["type"]}">\n{snapshot}\n</project>')
    if blocks:
        head = ("Fresh snapshots of the selected projects' agent terminals — "
                "snapshots in earlier messages are stale:\n" + "\n".join(blocks))
    else:
        head = ("No projects are selected, so no terminals are attached. Say so, "
                "and offer to select some.")
    if omitted:
        head += ("\n\nAlso selected, but too many terminals to attach at once: "
                 + ", ".join(p["name"] for p in omitted)
                 + ". read_terminal reaches any of them.")
    chosen = {p["name"] for p in watching_now}

    def state(p):
        live = [i["id"] for i in p["instances"] if alive(i["target"])]
        out = f"running ({', '.join(live)})" if live else "stopped"
        up = web_up(p)
        out = out if up is None else out + (", web up" if up else ", web down")
        pu = proj_usage.get(p["name"]) or {}
        parts = [f"{a} {pu[a]['share_7d_pct']:.0f}%" for a in ("claude", "agy")
                 if a in pu and pu[a].get("share_7d_pct", 0) >= 1]
        return out + (f" [7d: {', '.join(parts)}]" if parts else "")

    usnap = usage.snapshot(status_db())
    proj_usage = usnap["by_project"]
    roster = "\n".join(
        f"  {'*' if p['name'] in chosen else ' '} {state(p)}  {p['name']}"
        for p in load_projects())
    head += ("\n\nEvery project on this machine (* = selected, and so shown "
             f"above):\n{roster}")
    svc_lines = []
    for svc in ("claude", "agy"):
        s = usnap["services"].get(svc) or {}
        b_str = ", ".join(f"{b['label']} {b['remaining_pct']:.0f}% left"
                          for b in (s.get("buckets") or []))
        cred = s.get("credits") or {}
        if cred.get("kind") == "usd" and cred.get("remaining_dollars") is not None:
            c_str = f"${cred['remaining_dollars']:.2f}/${cred['limit_dollars']:.0f} grant left"
        elif cred.get("kind") == "credits" and cred.get("remaining_credits") is not None:
            c_str = f"{cred['remaining_credits']} credits left"
        else:
            c_str = ""
        line = " · ".join(x for x in (b_str, c_str) if x)
        if line:
            svc_lines.append(f"  {s.get('label', svc)}: {line}")
    if svc_lines:
        head += "\n\nService quotas & remaining credits:\n" + "\n".join(svc_lines)
    return head + "\n\n" + operator_instruction(mode, message) + (VOICE_NOTE if voice else "")


def operator_instruction(mode: str, message: str) -> str:
    if mode == "status":
        return ("Round-up please: across the selected projects, what is each one "
                "doing, and what needs me?")
    if mode == "brainstorm":
        base = message or ("Given where all of these stand, what should I pick "
                           "up next?")
        return "Let's brainstorm — discussion only, keep it conversational:\n" + base
    return message

def terminal_web() -> dict | None:
    """Where the browser terminal is served, from its registry entry."""
    name = CFG.get("terminal_project")
    p = next((x for x in load_projects() if x["name"] == name), None) if name else None
    return p["web"]["main"] if p and p.get("web") else None




SONNET_SUMMARY_PROMPT = """\
Here is the end of a coding agent's turn in the "{project}" project ({agent}).
Give me a status line for a dashboard: ONE line, at most 20 words, plain text,
no markdown. Say what the agent did this turn and, only if its final message
ends by asking me something now, what it asks.

<final_message>
{message}
</final_message>

<terminal_tail>
{pane}
</terminal_tail>"""


def sonnet_summary(eid: int) -> dict:
    """The optional upgrade: Sonnet's take on one turn, on request. A plain
    one-shot `claude -p` with no tools; if it fails, nothing else is touched
    and the local summary stays."""
    db = status_db()
    row = db.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
    if not row:
        raise KeyError(f"no event {eid}")
    prompt = SONNET_SUMMARY_PROMPT.format(
        project=row["project"], agent=AGENTS.get(row["instance"], {}).get("label", row["instance"]),
        message=summarize.clip_message(events.turn_text(row), 1000, 6000),
        pane=summarize.clean_pane(row["pane"] or "", 40))
    t0 = time.monotonic()
    data = claude_json(["--model", CFG["model"], "--allowedTools", ""], prompt,
                       str(ROOT), CFG["chat_timeout"])
    text = " ".join((data.get("result") or "").split())
    if not text:
        raise AdvisorError("Sonnet returned nothing")
    events.save_summary(db, eid, "sonnet", model=CFG["model"], summary=text[:300],
                        ms=int((time.monotonic() - t0) * 1000))
    return events.summary_for(db, eid)



# ── Reply watcher (summarize what the agent did with each prompt) ────────────
#
# A coding agent's TUI redraws constantly while it works (spinner, token
# counter), so "the pane stopped changing" is a reliable stand-in for "the agent
# is done and waiting". One watcher per project; a newer prompt supersedes the
# one in flight.

_watch_gen: dict[tuple[str, str], int] = defaultdict(int)
_watching: set[tuple[str, str]] = set()
_watch_guard = threading.Lock()


def watching(name: str, agent: str | None = None) -> bool:
    """Is a watcher running on this instance — or, with no agent named, on any
    of the project's instances?"""
    with _watch_guard:
        if agent is not None:
            return (name, agent) in _watching
        return any(n == name for n, _ in _watching)


def summarize_reply(project: dict, inst: dict, sent_text: str, pane: str,
                    settled: bool) -> str:
    """Ask the advisor — same session, so it already knows what was asked and
    why — to sum up what the agent did. This is the only summary there is: the
    page shows it and the phone reads it aloud."""
    name = project["name"]
    mode = "summary" if settled else "summary-partial"
    prompt = build_prompt(project, inst, mode, sent_text, pane)
    with chat_lock(name):  # never two turns on one advisor session at once
        reply, sid = run_advisor(project, prompt, STATE.get_sid(name),
                                 pick_model(mode, False), agent=inst["id"])
        if sid:
            STATE.set_sid(name, sid)
    return reply


def watch_reply(project: dict, inst: dict, sent_text: str, gen: int) -> None:
    name = project["name"]
    key = (name, inst["id"])
    poll = max(1.0, float(CFG["summary_poll"]))
    needed = max(1, round(float(CFG["summary_settle"]) / poll))
    deadline = time.monotonic() + float(CFG["summary_timeout"])
    pane, stable, settled = None, 0, False
    try:
        time.sleep(min(poll, 2.0))  # let the paste and the agent's start land
        while time.monotonic() < deadline:
            with _watch_guard:
                if _watch_gen[key] != gen:
                    return  # a newer prompt is in flight; that watcher owns it
            latest = capture_pane(inst["target"], CFG["pane_lines"])
            if latest is None:
                return  # session went away
            if latest == pane:
                stable += 1
                if stable >= needed:
                    settled = True
                    break
            else:
                pane, stable = latest, 0
            time.sleep(poll)
        if pane is None:
            return
        try:
            summary = summarize_reply(project, inst, sent_text, pane, settled)
        except (AdvisorError, subprocess.TimeoutExpired) as e:
            summary = f"(couldn't summarize the agent's reply: {e})"
        if summary:
            STATE.append(name, "summary", summary, agent=inst["id"])
    except Exception:  # noqa: BLE001 — a watcher must never take the server down
        traceback.print_exc()
    finally:
        with _watch_guard:
            if _watch_gen[key] == gen:
                _watching.discard(key)


# ── Background music (musicforprogramming.net) ───────────────────────────────

_music_cache: dict = {"ts": 0.0, "tracks": []}
_music_guard = threading.Lock()


def music_tracks() -> list[dict]:
    """Episode list from the musicforprogramming.net feed. Fetched here rather
    than in the browser because the feed sends no CORS header; the audio itself
    does, so the phone streams it straight from the source. Cached."""
    with _music_guard:
        cached, age = _music_cache["tracks"], time.time() - _music_cache["ts"]
        if cached and age < float(CFG["music_ttl"]):
            return cached
    req = urllib.request.Request(CFG["music_feed"],
                                 headers={"User-Agent": "voice_prompt"})
    with urllib.request.urlopen(req, timeout=20) as r:
        feed = r.read()
    tracks = []
    for item in ElementTree.fromstring(feed).iter("item"):
        enc = item.find("enclosure")
        url = enc.get("url") if enc is not None else None
        if not url or not (enc.get("type") or "").startswith("audio"):
            continue
        title = (item.findtext("title") or "").strip()
        tracks.append({"title": title or url.rsplit("/", 1)[-1], "url": url})
    with _music_guard:
        _music_cache.update(ts=time.time(), tracks=tracks)
    return tracks


def start_watch(project: dict, inst: dict, sent_text: str) -> None:
    if not CFG.get("summarize"):
        return
    if sent_text.lstrip().startswith("/"):
        return  # a slash command (/clear, /model) has no reply worth summarizing
    key = (project["name"], inst["id"])
    with _watch_guard:
        _watch_gen[key] += 1
        gen = _watch_gen[key]
        _watching.add(key)
    threading.Thread(target=watch_reply, args=(project, inst, sent_text, gen),
                     daemon=True).start()
# ── Local speech servers (STT / TTS) ─────────────────────────────────────────

# Proxied rather than called from the page directly: the speech servers are
# bound to localhost, the phone is not on this machine, and going through here
# means one origin, one token, and no CORS to arrange.

TTS_MIME = {"mp3": "audio/mpeg", "wav": "audio/wav", "opus": "audio/ogg",
            "flac": "audio/flac", "aac": "audio/aac", "pcm": "audio/L16"}

_voices_cache = {"ts": 0.0, "voices": []}
_voices_guard = threading.Lock()


class UpstreamError(RuntimeError):
    """A local speech server was unreachable, or answered with an error."""


def stt_enabled() -> bool:
    return bool((CFG.get("stt_url") or "").strip())


def tts_enabled() -> bool:
    return bool((CFG.get("tts_url") or "").strip())


def _upstream(url: str, data: bytes | None, headers: dict, timeout: float):
    """One call to a local speech server, with errors worth showing the user.
    A refused connection is the common one — the server just isn't running —
    and saying so beats a stack trace on the phone."""
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    host = urlparse(url).netloc
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace").strip().replace("\n", " ")
        raise UpstreamError(f"{host} returned HTTP {e.code}{': ' + detail if detail else ''}")
    except urllib.error.URLError as e:
        raise UpstreamError(f"can't reach {host} — {e.reason}")
    except TimeoutError:
        raise UpstreamError(f"{host} timed out")


def _multipart(fields: dict[str, str], name: str, filename: str,
               content_type: str, payload: bytes) -> tuple[bytes, str]:
    """Minimal multipart/form-data body — stdlib has no encoder for it."""
    boundary = "----voiceprompt" + os.urandom(9).hex()
    out = bytearray()
    for key, value in fields.items():
        out += (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n").encode()
    out += (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n").encode()
    out += payload
    out += f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def transcribe(audio: bytes) -> str:
    """Send recorded speech to the local STT server and get text back.

    The page always uploads 16 kHz mono WAV, which every whisper server takes
    without a decoder step — including whisper.cpp's, which takes nothing else.
    The form fields are the OpenAI transcription ones; whisper.cpp's /inference
    accepts the same shape, so stt_url can point at either."""
    url = (CFG.get("stt_url") or "").strip()
    if not url:
        raise UpstreamError("server transcription is off — set stt_url in config.toml")
    fields = {"response_format": "json"}
    if CFG.get("stt_model"):
        fields["model"] = str(CFG["stt_model"])
    if CFG.get("stt_language"):
        fields["language"] = str(CFG["stt_language"])
    body, ctype = _multipart(fields, "file", "speech.wav", "audio/wav", audio)
    raw, _ = _upstream(url, body, {"Content-Type": ctype,
                                   "Accept": "application/json"},
                       float(CFG["stt_timeout"]))
    try:
        data = json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace").strip()   # response_format=text
    if isinstance(data, dict):
        return str(data.get("text") or "").strip()
    return ""


def synthesize(text: str, voice: str, speed: float) -> tuple[bytes, str]:
    """One chunk of speech from the local TTS server (Kokoro, or anything else
    speaking OpenAI's /v1/audio/speech)."""
    url = (CFG.get("tts_url") or "").strip()
    if not url:
        raise UpstreamError("server speech is off — set tts_url in config.toml")
    fmt = str(CFG.get("tts_format") or "mp3")
    payload = json.dumps({
        "model": CFG.get("tts_model") or "kokoro",
        "input": text,
        "voice": voice or CFG.get("tts_voice") or "af_heart",
        "response_format": fmt,
        "speed": speed,
    }).encode()
    audio, ctype = _upstream(url, payload, {"Content-Type": "application/json"},
                             float(CFG["tts_timeout"]))
    if not audio:
        raise UpstreamError("the TTS server returned no audio")
    return audio, (ctype or TTS_MIME.get(fmt, "application/octet-stream"))


def tts_voices() -> list[str]:
    """The TTS server's voice list, for the picker in the web UI. Cached — the
    page asks on load and again whenever the settings sheet opens."""
    url = (CFG.get("tts_voices_url") or "").strip()
    if not url:
        url = re.sub(r"/audio/speech/?$", "/audio/voices", (CFG.get("tts_url") or "").strip())
    if not url:
        raise UpstreamError("server speech is off — set tts_url in config.toml")
    with _voices_guard:
        if _voices_cache["voices"] and time.time() - _voices_cache["ts"] < 300:
            return _voices_cache["voices"]
    raw, _ = _upstream(url, None, {"Accept": "application/json"}, 15)
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("voices", [])
    # Kokoro answers with a list of ids. Be tolerant: some builds send a list of
    # objects, and some an id-keyed map.
    if isinstance(data, dict):
        data = list(data.keys())
    voices = []
    for v in data if isinstance(data, list) else []:
        name = (v.get("id") or v.get("name")) if isinstance(v, dict) else v
        if name:
            voices.append(str(name))
    with _voices_guard:
        _voices_cache.update(ts=time.time(), voices=voices)
    return voices


# ── HTTP handler ─────────────────────────────────────────────────────────────

# Set by main() once the TLS listener is actually up; until then the plain
# listener serves the page rather than redirecting into a dead port.
HTTPS_READY = False
HTTPS_PORT = 0
HTTP_PORT = 0
BIND_ADDRS = ["127.0.0.1"]   # where main() listens (core/listen.py)


class Handler(BaseHTTPRequestHandler):
    server_version = "voice_prompt/1.0"
    protocol_version = "HTTP/1.1"

    # -- helpers ------------------------------------------------------------
    def _redirected_to_https(self) -> bool:
        """Bounce a plain-HTTP page load to the TLS listener; True if the reply
        is sent.

        The mic only works in a secure context, so the page has no business
        loading over http at all — telling people to retype the URL never
        worked. Three cases stay on http: TLS already (this handler serves
        both listeners), a proxy that terminated TLS for us (tailscale serve
        sets X-Forwarded-Proto), and localhost, which browsers already count
        as secure. No HSTS — the cert is self-signed, and HSTS would take away
        the one-tap "accept" that makes it usable.

        /api is exempt: a page served over https calls it same-origin anyway,
        and the non-browser callers (the MCP server, and any agent hooks)
        speak plain http and don't follow redirects.
        """
        if not HTTPS_READY or isinstance(self.connection, ssl.SSLSocket):
            return False
        if urlparse(self.path).path.startswith(("/api/", "/ws/")):
            return False
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            return False
        raw = (self.headers.get("Host") or "").strip()
        if raw.startswith("["):                                  # [::1]:8321
            host = raw[1:raw.index("]")] if "]" in raw else raw[1:]
            hostpart = f"[{host}]"
        else:
            host = hostpart = raw.split(":")[0]
        if not host or host in ("localhost", "::1") or host.startswith("127."):
            return False
        target = f"https://{hostpart}:{HTTPS_PORT}{self.path}"
        self.send_response(307)          # 307 keeps the method + body on POSTs
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return True

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data: bytes, content_type: str, code=200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        token = CFG.get("token") or ""
        if not token:
            return True
        got = self.headers.get("X-VP-Token", "")
        if not got:
            got = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        return hmac.compare_digest(got.encode(), token.encode())

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    def _raw_body(self, limit: int, what: str = "audio") -> bytes:
        """An upload that isn't JSON (recorded audio, an image). Read it whole
        either way — a body left in the socket would desync the next
        keep-alive request on this connection."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            # Hang up rather than read it: the point of the cap is not to.
            self.close_connection = True
            raise ValueError(f"{what} too large ({length} bytes, limit {limit})")
        data = self.rfile.read(length) if length > 0 else b""
        if not data:
            raise ValueError(f"no {what} in the upload")
        return data

    def log_message(self, fmt, *args):
        # Skip noisy pane-poll logging; keep the rest.
        if "/api/pane" not in (args[0] if args else ""):
            super().log_message(fmt, *args)

    # -- routes -------------------------------------------------------------
    def do_GET(self):
        if self._redirected_to_https():
            return
        path = urlparse(self.path).path
        if path == "/ws/attach":
            self._attach()
            return
        if path.startswith("/vendor/"):
            self._vendor(path[len("/vendor/"):])
            return
        if path == "/":
            try:
                body = INDEX_PATH.read_bytes()
            except OSError:
                self._json({"error": "static/index.html missing"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._authorized():
            self._json({"error": "unauthorized"}, 401)
            return
        if path in VOICE_GET and not VOICE_ON:
            self._json({"error": "the voice feature is off"}, 403)
            return
        try:
            if path == "/api/registry":
                self._json(registry_payload())
            elif path == "/api/tickets":
                self._json(tickets_payload())
            elif path == "/api/ticket":
                key = parse_qs(urlparse(self.path).query).get("key", [""])[0]
                self._json(ticket_payload(key))
            elif path == "/api/projects":
                out = []
                alive = alive_checker()
                usnap = usage.snapshot(status_db())
                for p in load_projects():
                    insts = overview_instances(p, alive)
                    out.append({"name": p["name"], "dir": p["dir"],
                                "target": p["target"], "web": p["web"],
                                "alive": any(i["alive"] for i in insts),
                                "instances": insts,
                                "usage": usnap["by_project"].get(p["name"])})
                self._json({"projects": out, "services": usnap["services"]})
            elif path == "/api/pane":
                q = parse_qs(urlparse(self.path).query)
                project = get_project(q.get("project", [""])[0])
                inst = get_instance(project, q.get("agent", [""])[0])
                lines = int(q.get("lines", [str(CFG["pane_lines"])])[0])
                text = capture_pane(inst["target"], lines)
                self._json({"alive": text is not None, "text": text or "",
                            "agent": inst["id"]})
            elif path == "/api/history":
                q = parse_qs(urlparse(self.path).query)
                name = q.get("project", [""])[0]
                self._json({"messages": STATE.history(name)})
            elif path == "/api/updates":
                # Summaries only — everything else the page already has, so this
                # can't duplicate bubbles it drew itself.
                q = parse_qs(urlparse(self.path).query)
                name = q.get("project", [""])[0]
                after = float(q.get("after", ["0"])[0] or 0)
                new = [m for m in STATE.history(name)
                       if m.get("role") == "summary" and m.get("ts", 0) > after]
                self._json({"summaries": new[-20:], "watching": watching(name)})
            elif path == "/api/overview":
                self._overview(parse_qs(urlparse(self.path).query))
            elif path == "/api/instances":
                # The CPU queue rides along, so each agent's card can say
                # whether it is waiting for cores or holding them.
                try:
                    cpu = CPU.state(timeout=1.5)
                except CPU.Unavailable as e:
                    cpu = {"error": str(e)}
                self._json(instances_payload() | {"cpu": cpu})
            elif path == "/api/cpu":
                try:
                    self._json(CPU.state())
                except CPU.Unavailable as e:
                    self._json({"error": str(e)}, 503)
            elif path == "/api/usage":
                self._json(usage.snapshot(status_db()))
            elif path == "/api/desk":
                q = parse_qs(urlparse(self.path).query)
                self._json(desk_payload(int(q.get("after", ["0"])[0] or 0)))
            elif path == "/api/event":
                q = parse_qs(urlparse(self.path).query)
                self._json(event_payload(int(q.get("id", ["0"])[0])))
            elif path == "/api/music":
                self._json({"tracks": music_tracks() if CFG.get("music") else []})
            elif path == "/api/voices":
                self._json({"voices": tts_voices()})
            elif path == "/api/meta":
                self._json({"voice": VOICE_ON,
                            "jira": REGISTRY.jira.enabled(),
                            "jira_url": REGISTRY.jira.settings()["url"].rstrip("/"),
                            "agents": [{"id": a, "label": m["label"]} for a, m in AGENTS.items()],
                            "default_agent": DEFAULT_AGENT,
                            "usage": bool(status.CFG.get("usage")),
                            "https_port": HTTPS_PORT,
                            "http_port": HTTP_PORT,
                            "summarize": bool(CFG.get("summarize")),
                            "music": bool(CFG.get("music")),
                            "stt": stt_enabled(),
                            "tts": tts_enabled(),
                            "tts_voice": str(CFG.get("tts_voice") or ""),
                            "tts_rate": STATE.tts_rate(),
                            "operator": operator_enabled(),
                            "operator_key": OPERATOR_KEY,
                            "terminal": terminal_web()})
            else:
                self._json({"error": "not found"}, 404)
        except KeyError as e:
            self._json({"error": str(e)}, 404)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except UpstreamError as e:
            self._json({"error": str(e)}, 502)
        except Exception as e:  # noqa: BLE001 — surface anything to the client
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        if self._redirected_to_https():
            return
        if not self._authorized():
            self._json({"error": "unauthorized"}, 401)
            return
        path = urlparse(self.path).path
        if path in VOICE_POST and not VOICE_ON:
            self._json({"error": "the voice feature is off"}, 403)
            return
        try:
            # Audio uploads first: the body is a WAV, not JSON.
            if path == "/api/stt":
                self._stt()
                return
            if path == "/api/image":
                self._image()
                return
            body = self._body()
            if path == "/api/chat":
                self._chat(body)
            elif path == "/api/send":
                self._send(body)
            elif path == "/api/tts":
                self._tts(body)
            elif path == "/api/tts/rate":
                rate = str(body.get("rate") or "")
                if rate not in TTS_RATES:
                    self._json({"error": f"unknown rate {rate!r}: use slow, medium or fast"}, 400)
                    return
                STATE.set_tts_rate(rate)
                self._json({"rate": rate})
            elif path == "/api/selection":
                self._selection(body)
            elif path == "/api/nav":
                self._nav(body)
            elif path == "/api/control":
                self._json({"ok": True, "message": control(
                    str(body.get("action") or ""), str(body.get("project") or ""),
                    str(body.get("agent") or DEFAULT_AGENT))})
            elif path == "/api/event/sonnet":
                self._json({"summary": sonnet_summary(int(body.get("id") or 0))})
            elif path == "/api/instances/dismiss":
                self._json(dismiss_instance(str(body.get("project") or ""),
                                            body.get("agent")))
            elif path == "/api/pin":
                self._json(set_pin(str(body.get("project") or ""),
                                   str(body.get("view") or ""), bool(body.get("pinned"))))
            elif path == "/api/cpu":
                # {verb: front|cancel|stop|cores, id, cores?} or {paused?, keep_free?}
                try:
                    if body.get("verb"):
                        self._json(CPU.act(str(body["verb"]), str(body.get("id") or ""),
                                           {"cores": body.get("cores")}))
                    else:
                        self._json(CPU.settings(body.get("paused"), body.get("keep_free")))
                except CPU.Unavailable as e:
                    self._json({"error": str(e)}, 503)
            elif path == "/api/usage/refresh":
                db = status_db()
                usage.sync_transcripts(db, load_projects())
                usage.sync_quotas(db, force=True)
                self._json(usage.snapshot(db))
            elif path == "/api/reset":
                STATE.reset(body["project"])
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except KeyError as e:
            self._json({"error": f"missing/unknown: {e}"}, 400)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except AdvisorError as e:
            self._json({"error": f"advisor failed: {e}"}, 502)
        except UpstreamError as e:
            self._json({"error": str(e)}, 502)
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # -- live terminals -----------------------------------------------------
    def _attach(self):
        """A terminal on one agent's session, for the Projects tab (live.py)."""
        if not self._authorized():
            self._json({"error": "unauthorized"}, 401)
            return
        if not live.wsproto.is_upgrade(self.headers):
            self._json({"error": "expected a WebSocket upgrade"}, 400)
            return
        q = parse_qs(urlparse(self.path).query)
        ws = live.handshake(self)
        try:
            project = get_project(q.get("project", [""])[0])
            inst = get_instance(project, q.get("agent", [""])[0] or None)
        except (KeyError, ValueError) as e:
            live.refuse(ws, str(e).strip("'\""))
            return
        if not session_alive(inst["target"]):
            live.refuse(ws, f"{project['name']}'s {inst['label']} session isn't running")
            return
        try:
            cols, rows = int(q.get("cols", ["80"])[0]), int(q.get("rows", ["24"])[0])
        except ValueError:
            cols, rows = 80, 24
        live.run(ws, inst["target"], cols, rows)

    def _vendor(self, name: str):
        """terminal/'s vendored xterm.js, by file name only."""
        target = VENDOR / name
        if "/" in name or not name or not target.is_file():
            self._json({"error": "not found"}, 404)
            return
        body = target.read_bytes()
        ctype = "text/css" if name.endswith(".css") else "text/javascript"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(body)

    # -- actions ------------------------------------------------------------
    def _chat(self, body):
        if str(body.get("scope") or "") == "operator":
            self._operator_chat(body)
            return
        project = get_project(body["project"])
        mode = body.get("mode", "discuss")
        message = (body.get("message") or "").strip()
        voice = bool(body.get("voice"))
        if mode == "discuss" and not message:
            self._json({"error": "empty message"}, 400)
            return
        try:
            inst = get_instance(project, body.get("agent"))
        except KeyError as e:
            self._json({"error": str(e).strip("'\"")}, 400)
            return
        name = project["name"]
        with chat_lock(name):
            pane = capture_pane(inst["target"], CFG["pane_lines"])
            prompt = build_prompt(project, inst, mode, message, pane, voice)
            label = {"status": "Status update",
                     "brainstorm": message or "Brainstorm next steps"}.get(mode, message)
            # Logged before the turn so anything the advisor sends mid-turn
            # lands after it in the history.
            STATE.append(name, "user", label, mode, agent=inst["id"])
            drain_advisor_sends(name)
            drain_nav(name)
            reply, sid = run_advisor(project, prompt, STATE.get_sid(name),
                                     pick_model(mode, voice), agent=inst["id"])
            sent = drain_advisor_sends(name)
            nav = drain_nav(name)
            desk_nav(nav)
            STATE.append(name, "advisor", reply)
            if sid:
                STATE.set_sid(name, sid)
        self._json({"reply": reply, "sent": sent, "nav": nav, "tts_rate": STATE.tts_rate()})

    def _operator_chat(self, body):
        """A turn with the multi-project operator. Same shape as a project turn
        — one conversation, resumed by session id — but the context is every
        selected project's terminal rather than one."""
        if not operator_enabled():
            self._json({"error": "multi-project mode is off (operator in config.toml)"}, 400)
            return
        mode = body.get("mode", "discuss")
        message = (body.get("message") or "").strip()
        voice = bool(body.get("voice"))
        if mode == "discuss" and not message:
            self._json({"error": "empty message"}, 400)
            return
        key = OPERATOR_KEY
        # The operator spans projects, so it has no project directory; it runs
        # in ours and reaches terminals through read_terminal instead.
        pseudo = {"name": key, "dir": str(ROOT), "target": None}
        with chat_lock(key):
            prompt = build_operator_prompt(mode, message, voice)
            label = {"status": "Round-up",
                     "brainstorm": message or "Brainstorm across projects"}.get(mode, message)
            STATE.append(key, "user", label, mode)
            drain_nav(key)
            reply, sid = run_advisor(pseudo, prompt, STATE.get_sid(key),
                                     pick_model(mode, voice), scope="operator")
            nav = drain_nav(key)
            desk_nav(nav)
            STATE.append(key, "advisor", reply)
            if sid:
                STATE.set_sid(key, sid)
        self._json({"reply": reply, "nav": nav, "tts_rate": STATE.tts_rate(),
                    "selected": [p["name"] for p in selected_projects()]})

    def _overview(self, query):
        """Everything the projects view draws: the roster with liveness and
        selection, plus summaries from the selected projects, so an agent
        finishing still reaches you when you're not inside its project."""
        after = float(query.get("after", ["0"])[0] or 0)
        chosen = set(STATE.selected())
        alive = alive_checker()
        usnap = usage.snapshot(status_db())
        # The Projects tab's pin, so the operator's roster tops with the same ones.
        pinned = {p["name"] for p in REGISTRY.load().get("projects", []) if p.get("pin_project")}
        projects, summaries = [], []
        for p in load_projects():
            picked = p["name"] in chosen
            insts = overview_instances(p, alive)
            projects.append({"name": p["name"], "dir": p["dir"],
                             "web": p["web"],
                             "alive": any(i["alive"] for i in insts),
                             "instances": insts,
                             "selected": picked,
                             "pinned": p["name"] in pinned,
                             "watching": watching(p["name"]),
                             "usage": usnap["by_project"].get(p["name"])})
            if not picked:
                continue
            for m in STATE.history(p["name"]):
                if m.get("role") == "summary" and m.get("ts", 0) > after:
                    summaries.append({"project": p["name"], "text": m.get("text", ""),
                                      "agent": m.get("agent"), "ts": m.get("ts", 0)})
        summaries.sort(key=lambda m: m["ts"])
        self._json({"projects": projects, "summaries": summaries[-20:],
                    "services": usnap["services"]})

    def _selection(self, body):
        """Set the working set. Replace with `selected`, or nudge it with
        `add`/`remove` — the tool takes the latter, so it doesn't have to read
        the current set back before changing one entry."""
        known = [p["name"] for p in load_projects()]
        if "selected" in body:
            want = [n for n in known if n in set(body.get("selected") or [])]
        else:
            want = [n for n in STATE.selected() if n in known]
            for n in (body.get("add") or []):
                if n in known and n not in want:
                    want.append(n)
            for n in (body.get("remove") or []):
                if n in want:
                    want.remove(n)
        STATE.set_selected(want)
        self._json({"selected": want, "known": known})

    def _nav(self, body):
        """An advisor asked for the user's screen to move. Held until the turn
        it was raised in finishes, so the view and the words arrive together."""
        view = str(body.get("view") or "")
        if view not in ("projects", "project"):
            self._json({"error": f"unknown view: {view!r}"}, 400)
            return
        nav = {"view": view}
        if view == "project":
            project = get_project(str(body.get("project") or ""))
            nav["project"] = project["name"]
            if body.get("agent"):
                nav["agent"] = get_instance(project, str(body["agent"]))["id"]
        note_nav(str(body.get("from") or "").strip() or OPERATOR_KEY, nav)
        self._json({"ok": True, "nav": nav})

    def _stt(self):
        """Transcribe recorded speech. The page posts a bare WAV rather than a
        JSON envelope — base64 in JSON would cost a third more bytes over the
        phone's link for no gain."""
        audio = self._raw_body(int(CFG["stt_max_bytes"]))
        self._json({"text": transcribe(audio)})

    def _image(self):
        """An image for an agent, from a phone's photos or clipboard: saved
        where that agent can read it (core/pastes.py), and the path handed
        back for the page to paste into its terminal."""
        q = parse_qs(urlparse(self.path).query)
        data = self._raw_body(PASTES.LIMIT, "image")
        inst = get_instance(get_project(q.get("project", [""])[0]), q.get("agent", [""])[0] or None)
        saved = PASTES.save(data)
        target = inst["target"]
        path = (PASTES.into_container(saved, target["container"])
                if target["type"] == "docker" else str(saved))
        self._json({"path": path})

    def _tts(self, body):
        text = (body.get("text") or "").strip()
        if not text:
            self._json({"error": "nothing to say"}, 400)
            return
        try:
            speed = float(body.get("speed") or 1.0)
        except (TypeError, ValueError):
            speed = 1.0
        audio, ctype = synthesize(text, str(body.get("voice") or ""),
                                  min(max(speed, 0.25), 4.0))
        self._bytes(audio, ctype)

    def _send(self, body):
        project = get_project(body["project"])
        text = (body.get("text") or "").strip()
        if not text:
            self._json({"error": "empty prompt"}, 400)
            return
        try:
            inst = get_instance(project, body.get("agent"))
        except KeyError as e:
            self._json({"error": str(e).strip("'\"")}, 400)
            return
        if not session_alive(inst["target"]):
            self._json({"error": f"{project['name']}'s {inst['label']} session "
                                 "isn't running — launch it first"}, 409)
            return
        send_to_project(inst["target"], text)
        STATE.append(project["name"], "sent", text, agent=inst["id"])
        if body.get("via") == "advisor":
            note_advisor_send(project["name"], {"text": text, "agent": inst["id"]})
        start_watch(project, inst, text)
        self._json({"ok": True, "agent": inst["id"],
                    "watching": watching(project["name"], inst["id"])})


# ── Main ─────────────────────────────────────────────────────────────────────

class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Suppress the routine noise: TLS handshake failures (plain HTTP hitting
        # the HTTPS port, cert pokes) and clients going away mid-response.
        exc = sys.exc_info()[0]
        if exc and issubclass(exc, (ssl.SSLError, ConnectionResetError,
                                    BrokenPipeError, TimeoutError)):
            return
        traceback.print_exc()


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def ensure_certs(cert: Path, key: Path) -> bool:
    """Generate a self-signed cert for the HTTPS listener if missing."""
    if cert.exists() and key.exists():
        return True
    cert.parent.mkdir(parents=True, exist_ok=True)
    san = f"subjectAltName=DNS:{socket.gethostname()},IP:{lan_ip()},IP:127.0.0.1"
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-days", "3650", "-subj", "/CN=voice-prompt", "-addext", san,
             "-keyout", str(key), "-out", str(cert)],
            capture_output=True, check=True, timeout=30)
        print(f"generated self-signed cert: {cert}")
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"warning: could not generate TLS cert ({e}); HTTPS disabled")
        return False


def ports(argv: list[str]) -> tuple[int, int]:
    """(https, http): --port / --http-port, then $PORT / $HTTP_PORT, then the
    registry entry this directory belongs to. No fallback — a server that
    can't find its port says so rather than binding a number of its own."""
    import argparse
    ap = argparse.ArgumentParser(description="Radio Room's voice server")
    ap.add_argument("--port", type=int, help="HTTPS port (default: $PORT, then the registry)")
    ap.add_argument("--http-port", type=int,
                    help="plain-HTTP redirector (default: $HTTP_PORT, then the "
                         "registry's web_extra named \"http\"; 0 for none)")
    args = ap.parse_args(argv)
    cfg = REGISTRY.load()
    entry = REGISTRY.find(cfg, radio_config.CFG["voice_project"])
    if entry is None:
        mine = REGISTRY.projects_for_dir(cfg, ROOT)
        entry = mine[0] if len(mine) == 1 else None
    https = args.port or int(os.environ.get("PORT") or 0) or (entry or {}).get("web_port")
    if not https:
        sys.exit(f"voice server: no port. Register the voice project "
                 f"({radio_config.CFG['voice_project']!r}) with a web_port, "
                 f"set $PORT, or pass --port.")
    http = args.http_port
    if http is None:
        http = int(os.environ.get("HTTP_PORT") or 0) or next(
            (x["port"] for x in (entry or {}).get("web_extra", [])
             if x.get("name") == "http" and x.get("port")), 0)
    return int(https), int(http)


def bind_addresses(cfg: dict) -> list[str]:
    """Where to listen, from [voice] bind — or exit saying why not. This
    server drives agents (Send, the advisor's send tool), so listening past
    this machine and the tailnet takes a token, or an explicit
    allow_no_token = true for a network that is trusted some other way."""
    try:
        addrs = LISTEN.addresses(cfg.get("bind"))
    except LISTEN.BindError as e:
        sys.exit(f"voice server: {e}")
    wide = LISTEN.exposed(addrs)
    if wide and not cfg.get("token") and not cfg.get("allow_no_token"):
        sys.exit(f"voice server: bind {', '.join(wide)} reaches past this machine and "
                 f"the tailnet, and anyone who reaches it can prompt your agents. Set "
                 f'[voice] token, bind = "local" or "tailscale", or allow_no_token = true '
                 f"if that network is trusted.")
    return addrs


def main():
    global HTTPS_READY, HTTPS_PORT, HTTP_PORT, BIND_ADDRS

    https_port, HTTP_PORT = ports(sys.argv[1:])
    BIND_ADDRS = bind_addresses(CFG)
    ip = lan_ip()
    print(f"radio-room voice: projects from {REGISTRY.CONFIG_PATH}")
    # The mic needs HTTPS, so that is the main listener; plain HTTP redirects
    # to it (and is what the MCP helper calls back on). Without a certificate
    # the main port serves plain HTTP instead.
    cert, key = ROOT / "certs" / "cert.pem", ROOT / "certs" / "key.pem"
    servers = []
    if ensure_certs(cert, key):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        for addr in BIND_ADDRS:
            httpsd = QuietServer((addr, https_port), Handler)
            httpsd.socket = ctx.wrap_socket(httpsd.socket, server_side=True)
            servers.append(httpsd)
        HTTPS_READY, HTTPS_PORT = True, https_port
        print(f"radio-room voice: https://{ip}:{https_port}  (self-signed)")
    else:
        HTTP_PORT = https_port
    if HTTP_PORT:
        servers += [QuietServer((addr, HTTP_PORT), Handler) for addr in BIND_ADDRS]
        print(f"radio-room voice: http://{ip}:{HTTP_PORT}"
              + ("  (redirects to https)" if HTTPS_READY else ""))
    print(f"radio-room voice: listening on {', '.join(BIND_ADDRS)}")

    status.start()
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

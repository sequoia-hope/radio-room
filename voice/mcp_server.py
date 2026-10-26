#!/usr/bin/env python3
"""Radio Room voice MCP server — gives the phone-side advisor a tool for submitting
prompts to the project's tmux agent, so work can be dispatched purely by voice.

server.py spawns one of these per advisor turn (stdio transport) via
`claude -p --mcp-config ...`. It doesn't drive tmux itself: it calls back into
the running voice_prompt HTTP server, so a tool send takes exactly the same
path as the web UI's "Send ➤" button (one writer for state.json, and the send
shows up in the chat log).

Environment:
  VP_PROJECT   project whose tmux session this server targets (required for a
               project-scoped advisor; for the operator it is the reserved
               conversation key instead)
  VP_SCOPE     "project" (default) or "operator" — which set of tools to publish
  VP_AGENT     the agent instance the user is focused on ("claude" or "agy");
               what project tools act on when the advisor doesn't name one
  VP_TOOLS     comma-separated tool names to publish; server.py decides, so
               --allowedTools and tools/list can never drift apart
  VP_BASE_URL  voice_prompt server URL (default: from config.toml, localhost)
  VP_TOKEN     shared secret (default: from config.toml)

Stdlib only. Anything printed on stdout must be a JSON-RPC message — log to
stderr instead.
"""

import json
import os
import sys
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
import cpu  # noqa: E402 — the CPU reservation queue, read straight from its server

ROOT = Path(__file__).resolve().parent

SERVER_INFO = {"name": "voice_prompt", "version": "1.0.0"}
LATEST_PROTOCOL = "2025-06-18"
KNOWN_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}

PANE_LINES_DEFAULT = 80
PANE_LINES_MAX = 400


def server_url_and_token() -> tuple[str, str]:
    """Where to call back to: VP_BASE_URL, which the server always passes. The
    token comes from Radio Room's config file rather than the environment, so it
    never has to ride along in the MCP subprocess's argv."""
    url = os.environ.get("VP_BASE_URL")
    if not url:
        sys.exit("mcp_server: VP_BASE_URL is not set (the voice server sets it)")
    token = os.environ.get("VP_TOKEN")
    if token is None:
        sys.path.insert(0, str(ROOT.parent / "core"))
        import config as radio_config
        token = str((radio_config.CFG.get("voice") or {}).get("token") or "")
    return url.rstrip("/"), token


BASE_URL, TOKEN = server_url_and_token()
PROJECT = os.environ.get("VP_PROJECT", "").strip()
SCOPE = os.environ.get("VP_SCOPE", "project").strip() or "project"
OPERATOR = SCOPE == "operator"
AGENT = os.environ.get("VP_AGENT", "").strip() or "claude"
# Empty means "everything this scope defines" — only useful when running the
# MCP server by hand.
ALLOWED = {t for t in os.environ.get("VP_TOOLS", "").split(",") if t}


# ── voice_prompt HTTP API ────────────────────────────────────────────────────

# The server is on this machine with a self-signed certificate; when it has no
# plain-HTTP listener, the callback goes over its HTTPS one unverified.
LOOPBACK_TLS = ssl._create_unverified_context()


class ToolError(RuntimeError):
    pass


def api(path: str, payload: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE_URL + path, data=data,
                                 method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("X-VP-Token", TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=LOOPBACK_TLS) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body).get("error") or body
        except json.JSONDecodeError:
            msg = body
        raise ToolError(msg.strip() or f"HTTP {e.code}")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        raise ToolError(f"cannot reach the voice_prompt server at {BASE_URL}: {e}")


# ── Tools ────────────────────────────────────────────────────────────────────

# Which of a project's agents a tool acts on. A project can run Claude Code in
# its base tmux session and Antigravity (agy) beside it.
AGENT_PROP = {"agent": {
    "type": "string",
    "enum": ["claude", "agy"],
    "description": (
        "Which of the project's agents: claude (Claude Code) or agy "
        "(Antigravity). Leave it out for the one the user is focused on"
        + (" — for the operator, Claude Code." if OPERATOR else
           f" ({AGENT}).")
    ),
}}

TOOLS = [
    {
        "name": "send_prompt",
        "description": (
            "Submit a prompt to the coding agent running in this project's tmux "
            "session: the text is typed into its terminal and Enter is pressed, "
            "exactly as if the user had typed it. This is how the user — who is "
            "on a phone, hands-free — dispatches work. Use it only when they "
            "have asked you to send/tell/ask the agent something. Pass the "
            "finished instruction alone: no preamble, no surrounding quotes, no "
            "commentary about sending it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "The exact text to submit, written as if speaking "
                        "directly to the agent."
                    ),
                },
                **AGENT_PROP,
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "clear_agent_context",
        "description": (
            "Submit /clear to the coding agent's session, wiping the context it "
            "is holding — it starts the next prompt knowing nothing of what came "
            "before. Its scrollback stays on screen. Use when the user says the "
            "agent is out of context, confused, or should start fresh. This "
            "touches the AGENT only; your own conversation is not yours to "
            "clear — the user does that on the page."
        ),
        "inputSchema": {"type": "object", "properties": {**AGENT_PROP},
                        "additionalProperties": False},
    },
    {
        "name": "set_agent_model",
        "description": (
            "Submit /model <name> to the coding agent's session, changing which "
            "model the AGENT writes code with (e.g. opus for hard work, sonnet "
            "for routine edits). Whether a name is accepted is the agent's call "
            "— its terminal is where that shows. This does not change the model "
            "you run on. Names differ by agent: Claude Code takes opus / sonnet "
            "/ haiku; Antigravity takes ids like claude-opus-5-5-medium, "
            "gemini-4-argon-high or gemini-3.8-flash-low — and on Antigravity "
            "the choice also becomes the default for every new agy session, "
            "so say so when you use it there."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": ("Model name to give the agent, e.g. opus, "
                                    "sonnet, or for agy gemini-4-argon-medium."),
                },
                **AGENT_PROP,
            },
            "required": ["model"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_terminal",
        "description": (
            "Capture a project's terminal right now. Every message you get "
            "already carries a fresh snapshot of each selected project, so use "
            "this for a project that isn't selected, or to see what changed "
            "while you were waiting."
            if OPERATOR else
            "Capture the agent's terminal right now. Each of the user's messages "
            "already carries a fresh snapshot, so only use this to see what "
            "changed after you sent a prompt or while waiting on the agent, "
            "or to look at the project's other agent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **({"project": {"type": "string",
                                "description": "Project name, as listed to you."}}
                   if OPERATOR else {}),
                **AGENT_PROP,
                "lines": {
                    "type": "integer",
                    "description": (
                        f"Lines of scrollback to capture "
                        f"(default {PANE_LINES_DEFAULT}, max {PANE_LINES_MAX})."
                    ),
                },
            },
            "required": ["project"] if OPERATOR else [],
            "additionalProperties": False,
        },
    },
    {
        "name": "open_projects_view",
        "description": (
            "Take the user's screen back to the multi-project overview, where "
            "the operator watches every selected project at once. Use it when "
            "they say to go back, show all the projects, or leave this one. "
            "This conversation is not ended or cleared — coming back to this "
            "project later resumes exactly here."
        ),
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
    },
    {
        "name": "select_projects",
        "description": (
            "Set which projects you are watching. Their terminals ride along "
            "with every message you get, and the user's screen shows them as "
            "selected. Use it when they say which projects to watch or stop "
            "watching."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "projects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Project names, exactly as listed to you.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["only", "add", "remove"],
                    "description": (
                        "only (default) watches exactly these and nothing else; "
                        "add keeps the current set and adds them; remove drops "
                        "them from it."
                    ),
                },
            },
            "required": ["projects"],
            "additionalProperties": False,
        },
    },
    {
        "name": "open_project",
        "description": (
            "Switch the user's screen into one project, where a per-project "
            "advisor takes over and can dispatch work to that agent. Use it "
            "when they say to go into / open / switch to / work on a project, "
            "or when what they want is for an agent to actually do something — "
            "you cannot send prompts yourself. Say in your reply that you're "
            "taking them there; the screen changes under them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "Project name, exactly as listed to you."},
                "agent": {**AGENT_PROP["agent"], "description": (
                    "Which of its agents to focus on, when they named one: "
                    "claude or agy. Leave it out otherwise.")},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
]


TOOLS.append({
    "name": "set_speech_speed",
    "description": (
        "Set how fast your spoken replies are read out on the user's page: "
        "slow, medium or fast — the same three as the speed buttons there. "
        "Use it when they ask you to talk slower, faster or normally. It "
        "applies from this reply on, and the buttons show the new choice."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"speed": {"type": "string", "enum": ["slow", "medium", "fast"]}},
        "required": ["speed"],
        "additionalProperties": False,
    },
})


def lifecycle_tool(name: str, description: str, agent: bool) -> dict:
    props = {"project": {"type": "string",
                         "description": "Project name, exactly as listed to you."}}
    if agent:
        props["agent"] = {**AGENT_PROP["agent"], "description": (
            "claude (Claude Code, the default) or agy (Antigravity).")}
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props,
                            "required": ["project"], "additionalProperties": False}}


TOOLS += [
    lifecycle_tool("start_agent", (
        "Start a project's agent session the way the launcher does, if it "
        "isn't already running. It starts idle, waiting for a prompt; "
        "open_project afterwards if the user wants to work in it."), True),
    lifecycle_tool("stop_agent", (
        "Kill a project's agent session, and the agent in it — mid-turn or "
        "not. Only when the user asked to stop that project's agent."), True),
    lifecycle_tool("start_web", (
        "Bring a project's web app up, the way the hub does. It opens on the "
        "user's desktop window once it's up."), False),
    lifecycle_tool("stop_web", "Take a project's web app down.", False),
]

TOOLS += [
    {
        "name": "cpu_queue",
        "description": (
            "The machine's CPU reservation queue: how many cores are booked, "
            "which jobs are running and which are waiting, in order, each "
            "with its project and id. Heavy builds and test suites book cores "
            "here and wait their turn; a project 'stuck' on a build is often "
            "waiting in this line."),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "cpu_control",
        "description": (
            "Steer the CPU queue. stop: end a running job (its command is "
            "killed). cancel: drop a waiting job. front: move a waiting job to "
            "the head of the line. cores: change how many cores a waiting job "
            "asks for. pause / resume: hold or restart granting (running jobs "
            "carry on). keep_free: how many cores are never booked. Take ids "
            "from cpu_queue; stop only a job they asked you to stop."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["stop", "cancel", "front", "cores",
                                                      "pause", "resume", "keep_free"]},
                "id": {"type": "string", "description": "The job's id, from cpu_queue."},
                "cores": {"type": "integer", "minimum": 0,
                          "description": "For cores: the new count. For keep_free: the cores to keep free."},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
]

TOOLS = [t for t in TOOLS if not ALLOWED or t["name"] in ALLOWED]


def require_project() -> str:
    if not PROJECT:
        raise ToolError("no project configured for this MCP server (VP_PROJECT unset)")
    return PROJECT


AGENT_NAMES = {"claude": "Claude Code", "agy": "Antigravity"}


def pick_agent(args: dict) -> str:
    agent = (args.get("agent") or "").strip() or ("claude" if OPERATOR else AGENT)
    if agent not in AGENT_NAMES:
        raise ToolError(f"unknown agent {agent!r} — use claude or agy")
    return agent


def send(project: str, agent: str, text: str) -> None:
    api("/api/send", {"project": project, "agent": agent, "text": text,
                      "via": "advisor"})


def tool_send_prompt(args: dict) -> str:
    project = require_project()
    agent = pick_agent(args)
    text = (args.get("prompt") or "").strip()
    if not text:
        raise ToolError("prompt is empty — nothing to send")
    send(project, agent, text)
    return (f"Submitted to the {project} {AGENT_NAMES[agent]} agent (typed into "
            f"its tmux session and entered):\n\n{text}")


def tool_read_terminal(args: dict) -> str:
    project = (args.get("project") or "").strip() if OPERATOR else require_project()
    if not project:
        raise ToolError("which project? pass its name")
    try:
        lines = int(args.get("lines") or PANE_LINES_DEFAULT)
    except (TypeError, ValueError):
        lines = PANE_LINES_DEFAULT
    lines = max(1, min(lines, PANE_LINES_MAX))
    agent = pick_agent(args)
    who = f"{project} {AGENT_NAMES[agent]}"
    q = urlencode({"project": project, "agent": agent, "lines": lines})
    data = api(f"/api/pane?{q}")
    if not data.get("alive"):
        return f"The {who} tmux session isn't running."
    return (f"Current {who} terminal:\n<terminal_snapshot>\n"
            f"{data.get('text') or '(empty)'}\n</terminal_snapshot>")


def tool_clear_agent_context(args: dict) -> str:
    project = require_project()
    agent = pick_agent(args)
    send(project, agent, "/clear")
    return (f"Submitted /clear to the {project} {AGENT_NAMES[agent]} agent — its "
            "context is wiped; it starts the next prompt fresh.")


def tool_set_agent_model(args: dict) -> str:
    project = require_project()
    agent = pick_agent(args)
    model = (args.get("model") or "").strip()
    if not model:
        raise ToolError("model is required (e.g. opus, sonnet)")
    send(project, agent, f"/model {model}")
    out = (f"Submitted /model {model} to the {project} {AGENT_NAMES[agent]} "
           "agent. Its terminal will show whether it took — the agent decides "
           "which names it accepts.")
    if agent == "agy":
        # Measured, not guessed: agy writes the pick into its settings.json.
        out += (" Antigravity also saves this as its default model, so new agy "
                "sessions in every project will start on it — tell the user.")
    return out


def tool_open_projects_view(args: dict) -> str:
    api("/api/nav", {"from": PROJECT, "view": "projects"})
    return ("The user's screen is going back to the projects view when this turn "
            "ends. This conversation stays as it is. Tell them you're taking "
            "them back.")


def tool_open_project(args: dict) -> str:
    name = (args.get("project") or "").strip()
    if not name:
        raise ToolError("project is required")
    nav = {"from": PROJECT, "view": "project", "project": name}
    agent = (args.get("agent") or "").strip()
    if agent:
        nav["agent"] = pick_agent(args)
    data = api("/api/nav", nav)
    opened = (data.get("nav") or {}).get("project", name)
    if agent:
        opened += f" ({AGENT_NAMES[nav['agent']]})"
    return (f"The user's screen is switching into {opened} when this turn ends; "
            f"its own advisor picks up from there, with its own history. Tell "
            f"them you're taking them in.")


def tool_select_projects(args: dict) -> str:
    names = [str(n).strip() for n in (args.get("projects") or []) if str(n).strip()]
    if not names:
        raise ToolError("projects is required — name at least one")
    mode = (args.get("mode") or "only").strip()
    key = {"only": "selected", "add": "add", "remove": "remove"}.get(mode)
    if not key:
        raise ToolError(f"unknown mode {mode!r} — use only, add or remove")
    data = api("/api/selection", {key: names})
    now = data.get("selected") or []
    unknown = [n for n in names if n not in (data.get("known") or [])]
    out = ("Now watching: " + ", ".join(now)) if now else "Watching nothing now."
    if unknown:
        out += (f"\n\nNo such project: {', '.join(unknown)} — ignored. The "
                "roster in your last message has the real names.")
    return out


def tool_set_speech_speed(args: dict) -> str:
    speed = (args.get("speed") or "").strip()
    if speed not in ("slow", "medium", "fast"):
        raise ToolError("speed must be slow, medium or fast")
    api("/api/tts/rate", {"rate": speed})
    return f"Spoken replies are now {speed}, starting with this one."


def lifecycle(action: str, args: dict) -> str:
    name = (args.get("project") or "").strip()
    if not name:
        raise ToolError("project is required")
    body = {"action": action, "project": name}
    if action.endswith("_agent"):
        body["agent"] = pick_agent(args)
    # Start scripts can take a while; the registry gives them two minutes.
    return api("/api/control", body, timeout=150).get("message") or "done"


def tool_cpu_queue(args: dict) -> str:
    try:
        return cpu.describe(cpu.state())
    except cpu.Unavailable as e:
        raise ToolError(str(e))


def tool_cpu_control(args: dict) -> str:
    action = args.get("action")
    try:
        if action in ("pause", "resume"):
            cpu.settings(paused=action == "pause")
        elif action == "keep_free":
            if args.get("cores") is None:
                raise ToolError("keep_free needs cores")
            cpu.settings(keep_free=int(args["cores"]))
        else:
            if not args.get("id"):
                raise ToolError(f"{action} needs the job's id, from cpu_queue")
            body = {"cores": int(args["cores"])} if action == "cores" else None
            if action == "cores" and not args.get("cores"):
                raise ToolError("cores needs the new count")
            if not cpu.act(action, args["id"], body).get("ok"):
                raise ToolError(f"couldn't {action} {args['id']}: no such job, or it isn't "
                                + ("running" if action == "stop" else "waiting"))
        return "Done. The queue now:\n" + cpu.describe(cpu.state())
    except cpu.Unavailable as e:
        raise ToolError(str(e))


HANDLERS = {"send_prompt": tool_send_prompt,
            "cpu_queue": tool_cpu_queue, "cpu_control": tool_cpu_control, "read_terminal": tool_read_terminal,
            "clear_agent_context": tool_clear_agent_context,
            "set_agent_model": tool_set_agent_model,
            "open_projects_view": tool_open_projects_view,
            "open_project": tool_open_project,
            "select_projects": tool_select_projects,
            "set_speech_speed": tool_set_speech_speed,
            **{a: (lambda args, a=a: lifecycle(a, args))
               for a in ("start_agent", "stop_agent", "start_web", "stop_web")}}


# ── JSON-RPC / MCP plumbing ──────────────────────────────────────────────────

def call_tool(params: dict) -> dict:
    name = params.get("name")
    handler = HANDLERS.get(name)
    if not handler:
        return {"content": [{"type": "text", "text": f"unknown tool: {name}"}],
                "isError": True}
    try:
        text = handler(params.get("arguments") or {})
    except ToolError as e:
        return {"content": [{"type": "text", "text": f"Failed: {e}"}], "isError": True}
    except Exception as e:  # noqa: BLE001 — report, don't kill the server
        return {"content": [{"type": "text", "text": f"Failed: {type(e).__name__}: {e}"}],
                "isError": True}
    return {"content": [{"type": "text", "text": text}]}


def dispatch(method: str, params: dict) -> dict:
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {
            "protocolVersion": asked if asked in KNOWN_PROTOCOLS else LATEST_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        return call_tool(params)
    if method == "ping":
        return {}
    if method in ("resources/list", "resources/templates/list"):
        return {"resources": [], "resourceTemplates": []}
    if method == "prompts/list":
        return {"prompts": []}
    raise LookupError(method)


def main() -> None:
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "parse error"}}
            out.write(json.dumps(reply) + "\n")
            out.flush()
            continue
        mid = msg.get("id")
        if mid is None:  # notification — acknowledged by silence
            continue
        try:
            result = dispatch(msg.get("method") or "", msg.get("params") or {})
            reply = {"jsonrpc": "2.0", "id": mid, "result": result}
        except LookupError as e:
            reply = {"jsonrpc": "2.0", "id": mid,
                     "error": {"code": -32601, "message": f"method not found: {e}"}}
        except Exception as e:  # noqa: BLE001
            reply = {"jsonrpc": "2.0", "id": mid,
                     "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        out.write(json.dumps(reply) + "\n")
        out.flush()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass

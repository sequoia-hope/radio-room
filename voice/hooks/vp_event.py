#!/usr/bin/env python3
"""Hook for Claude Code, Antigravity and Copilot CLI: turn a lifecycle hook
into a status event in Radio Room's store (events.py).

    vp_event.py claude              from ~/.claude/settings.json
    vp_event.py agy <Event>         from ~/.gemini/config/hooks.json
    vp_event.py copilot <event>     from ~/.copilot/hooks/radio-room.json

Claude Code names the event in its payload (hook_event_name); Antigravity's
and Copilot's payloads don't, so their configs pass the event name as the
second argument.

The payload arrives as JSON on stdin. Which project fired it comes from
$TMUX_PANE: the hook runs inside the agent's pane, so the tmux session name is
exact, and the server's session map turns that into a project and instance.
In a container (VP_CONTAINER set, voice/ mounted at its host path) the name
is prefixed with the container's, as the server's map has it.

This runs inside the agent's own loop — agy's hooks block it outright — so it
must be quick and must never fail the agent: every error is swallowed, the
exit code is always 0. Antigravity requires a JSON object on stdout; Claude
Code gets nothing, because whatever a UserPromptSubmit hook prints is added to
the user's prompt.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def reply(agent: str) -> None:
    if agent == "agy":
        sys.stdout.write("{}")
        sys.stdout.flush()


def tmux(*args: str) -> str:
    import subprocess
    try:
        p = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout if p.returncode == 0 else ""


# Claude Code hook -> (event, info). Stop is special-cased below.
CLAUDE_EVENTS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "prompt",
    "Stop": "stop",
    "StopFailure": "stop_error",
    "Notification": "notify",
    "SessionEnd": "session_end",
}

# Antigravity's Stop carries why the loop ended.
AGY_STOP = {
    "NO_TOOL_CALL": "stop",
    "TERMINAL_STEP_TYPE": "stop",
    "INJECTED_RESPONSE": "stop",
    "TERMINAL_CUSTOM_HOOK": "stop",
    "USER_CANCELED": "cancelled",
    "ERROR": "stop_error",
    "MAX_INVOCATIONS": "stop_error",
    "MAX_FORCED_INVOCATIONS": "stop_error",
    "MAX_TOKEN_BUDGET_EXCEEDED": "stop_error",
}


# Copilot CLI hook (camelCase payloads) -> event.
COPILOT_EVENTS = {
    "sessionStart": "session_start",
    "userPromptSubmitted": "prompt",
    "agentStop": "stop",
    "errorOccurred": "stop_error",
    "notification": "notify",
    "sessionEnd": "session_end",
}


def classify(agent: str, name: str, data: dict) -> tuple[str | None, dict]:
    """The normalized event for this hook, and what goes into its detail."""
    model = data.get("model") or data.get("modelName")
    if agent == "claude":
        event = CLAUDE_EVENTS.get(name)
        info = {"model": model} if model else {}
        if name == "SessionStart":
            info["source"] = data.get("source")
        elif name == "Notification":
            info["kind"] = data.get("notification_type")
            info["text"] = data.get("message")
        elif name == "Stop":
            info["background_tasks"] = len(data.get("background_tasks") or [])
        elif name == "StopFailure":
            info["error"] = data.get("error") or data.get("message")
        elif name == "SessionEnd":
            info["reason"] = data.get("reason")
        return event, info
    if agent == "copilot":
        # A subagent's hooks say whose they are; only the main agent's turn
        # is the instance's.
        if data.get("parentAgentId") or data.get("parent_agent_id"):
            return None, {}
        event = COPILOT_EVENTS.get(name)
        info = {"model": model} if model else {}
        if name == "sessionStart":
            info["source"] = data.get("source")
        elif name == "notification":
            info["kind"] = data.get("notification_type")
            info["text"] = data.get("message")
        elif name == "errorOccurred":
            if data.get("recoverable"):
                return None, {}
            err = data.get("error")
            info["error"] = err.get("message") if isinstance(err, dict) else err
        elif name == "sessionEnd":
            info["reason"] = data.get("reason")
        return event, info
    base = {"model": model} if model else {}
    if name == "SessionStart":
        return "session_start", {**base, "source": "startup"}
    if name == "PreInvocation":
        return "prompt", base
    if name == "Stop":
        reason = data.get("terminationReason") or ""
        info = {**base, "reason": reason, "fully_idle": data.get("fullyIdle")}
        if data.get("error"):
            info["error"] = data["error"]
        return AGY_STOP.get(reason, "stop"), info
    return None, {}


def main() -> None:
    agent = sys.argv[1] if len(sys.argv) > 1 else "claude"
    # Not an agent anyone is watching: the voice advisor's own turns, and
    # headless runs outside tmux (scripts, `claude -p` from a plain shell).
    if os.environ.get("VP_STATUS_HOOK") == "off" or not os.environ.get("TMUX_PANE"):
        return
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
    name = data.get("hook_event_name") or (sys.argv[2] if len(sys.argv) > 2 else "")

    # agy calls PreInvocation before every model call in a turn; only the
    # first one (invocationNum 0) means a prompt just went in. Leave before
    # paying for sqlite or tmux on the rest.
    if agent == "agy" and name == "PreInvocation" and data.get("invocationNum", 0) != 0:
        return

    event, info = classify(agent, name, data)
    if not event:
        return

    sys.path.insert(0, str(ROOT))
    import events

    pane_id = os.environ.get("TMUX_PANE", "")
    session = tmux("display-message", "-p", "-t", pane_id, "#{session_name}").strip() \
        if pane_id else ""
    # Inside a container the tmux server is the container's own; the server
    # maps such sessions as "container/name", and VP_CONTAINER says which.
    if session and os.environ.get("VP_CONTAINER"):
        session = f"{os.environ['VP_CONTAINER']}/{session}"
    pane = None
    if event in ("stop", "stop_error", "notify", "cancelled"):
        pane = tmux("capture-pane", "-p", "-J", "-t", pane_id,
                    "-S", f"-{events.PANE_KEEP}") if pane_id else None

    transcript = data.get("transcript_path") or data.get("transcriptPath")
    message = None
    if event in ("stop", "stop_error"):
        if agent == "claude":
            message = data.get("last_assistant_message") or \
                (events.claude_final_message(transcript) if transcript else "")
        elif agent == "copilot":
            message = events.copilot_final_message(transcript) if transcript else ""
        else:
            message = events.agy_final_message(transcript) if transcript else ""

    db = events.connect()
    project, instance, agent_type = events.resolve_session(db, session)
    events.record(
        db, event=event, source="hook", raw_event=name,
        project=project, instance=instance,
        agent_type=agent_type or {"claude": "claude-code", "agy": "antigravity"}.get(agent, agent),
        session=session or None, message=message, transcript_path=transcript,
        pane=pane, agent_session=(data.get("session_id") or data.get("sessionId")
                                  or data.get("conversationId")),
        cwd=data.get("cwd") or ((data.get("workspacePaths") or [None])[0]),
        detail={k: v for k, v in info.items() if v not in (None, "")},
    )


if __name__ == "__main__":
    agent = sys.argv[1] if len(sys.argv) > 1 else "claude"
    try:
        main()
    except Exception as e:  # noqa: BLE001 — never fail the agent over a status event
        try:
            with open(ROOT / "hooks" / "errors.log", "a") as f:
                f.write(f"{agent} {type(e).__name__}: {e}\n")
        except OSError:
            pass
    reply(agent)
    sys.exit(0)

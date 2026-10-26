"""Agent status events — the store and the state machine, shared by the hook
script (hooks/vp_event.py, run by Claude Code and Antigravity themselves) and
the server (its pane monitor, and the overview's reads and dismissals).

Everything lands in one SQLite file next to this module. The hook writes to it
directly rather than posting to the server: it then works with the server
down, needs no port, and costs an agent only a process start and an insert.

Tables
  events     one row per thing that happened to an instance: what, when, the
             agent's final message, its transcript, and the tail of its pane
  instances  each instance's current state and when it entered it
  sessions   tmux session -> (project, instance), kept by the server, so a hook
             can say which project fired it without parsing launch scripts
  summaries  one-line rough summaries of a turn, from a local model (and,
             when asked for, Sonnet) — see summarize.py

States
  starting     a session just started; nothing asked of it yet
  working      a prompt went in and the agent is on it
  needs_input  the agent stopped on a question, or is holding a menu/permission
  completed    the agent finished something you haven't looked at
  idle         nothing pending — dismissed, or cleared, or cancelled
  error        the turn ended in an error
  offline      the agent exited (the server also reports this for a dead
               tmux session, without storing it)

Stdlib only: the hook runs under whatever python3 the agent's shell finds.
"""

import json
import os
import re
import sqlite3
import time
from pathlib import Path

# VP_EVENTS_DB points tests at a scratch store; nothing else should set it.
DB_PATH = Path(os.environ.get("VP_EVENTS_DB")
               or Path(__file__).resolve().parent / "events.db")

STATES = ("starting", "working", "needs_input", "completed", "idle", "error",
          "offline")
# What a dismissal clears. Anything else is either still live (working) or
# already quiet, and dismissing it would only lie about it.
DISMISSABLE = ("completed", "needs_input", "error")

PANE_KEEP = 60          # lines of pane kept with each event
RETENTION_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY,
    ts              REAL NOT NULL,
    project         TEXT,
    instance        TEXT,
    agent_type      TEXT,
    session         TEXT,
    event           TEXT NOT NULL,   -- normalized, see transition()
    raw_event       TEXT,            -- the hook's own name for it
    source          TEXT NOT NULL,   -- hook | pane | app
    state           TEXT,            -- state this moved the instance to
    message         TEXT,            -- the agent's final message, if any
    transcript_path TEXT,
    pane            TEXT,            -- last lines of the pane, plain text
    agent_session   TEXT,            -- the agent's own session/conversation id
    cwd             TEXT,
    detail          TEXT             -- JSON: anything else worth keeping
);
CREATE INDEX IF NOT EXISTS events_inst ON events(project, instance, ts);
CREATE INDEX IF NOT EXISTS events_session ON events(session, ts);

CREATE TABLE IF NOT EXISTS instances (
    project     TEXT NOT NULL,
    instance    TEXT NOT NULL,
    agent_type  TEXT,
    session     TEXT,
    state       TEXT NOT NULL,
    since       REAL NOT NULL,       -- when it entered this state
    updated     REAL NOT NULL,       -- last event of any kind
    last_event  INTEGER,             -- the event that set the state
    last_hook   REAL,                -- last event that came from a hook
    detail      TEXT,                -- JSON: badges (background tasks, ...)
    PRIMARY KEY (project, instance)
);

CREATE TABLE IF NOT EXISTS summaries (
    event_id    INTEGER NOT NULL,
    source      TEXT NOT NULL,       -- granite (local) | sonnet (on request)
    ts          REAL NOT NULL,
    model       TEXT,
    status      TEXT,                -- the model's guess; never sets the state
    summary     TEXT,
    ms          INTEGER,
    error       TEXT,
    PRIMARY KEY (event_id, source)
);

CREATE TABLE IF NOT EXISTS sessions (
    session     TEXT PRIMARY KEY,    -- "name" local, "container/name" docker
    project     TEXT NOT NULL,
    instance    TEXT NOT NULL,
    agent_type  TEXT NOT NULL,
    updated     REAL NOT NULL
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    """A connection with the settings every writer needs. WAL lets the server
    read while a hook writes; the busy timeout makes two writers queue rather
    than fail."""
    db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    return db


def session_key(target: dict) -> str:
    """How a tmux target is named in the sessions table."""
    if target.get("type") == "docker":
        return f"{target['container']}/{target['session']}"
    return target["session"]


# ── Reading an agent's last word ─────────────────────────────────────────────

def _tail_lines(path: str, max_bytes: int = 512 * 1024) -> list[str]:
    """The last lines of a (possibly large) JSONL file, newest last. The first
    line of the chunk is dropped: it is almost always cut mid-record."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    return lines


def _tail_records(path: str):
    """Transcript lines newest first, reading further back only as needed.
    Claude's transcripts run to tens of MB and a single request blob can be
    hundreds of KB, so a fixed tail may hold nothing but tool calls."""
    seen = 0
    for size in (256 * 1024, 2 * 1024 * 1024, 8 * 1024 * 1024):
        lines = _tail_lines(path, size)
        for line in reversed(lines[:len(lines) - seen]):
            yield line
        if len(lines) == seen or len(lines) < 2:
            return
        seen = len(lines)


def claude_final_message(transcript_path: str) -> str:
    """Claude Code's final reply, from the end of its JSONL transcript — the
    fallback for when the Stop payload didn't carry last_assistant_message."""
    for line in _tail_records(transcript_path):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "text"]
            text = "\n".join(t for t in texts if t.strip()).strip()
            if text:
                return text
    return ""


def agy_final_message(transcript_path: str) -> str:
    """Antigravity's final reply: the newest model response with text in it.
    Its transcript stores each field JSON-encoded, so content is a string."""
    for line in _tail_records(transcript_path):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("source") != "MODEL" or rec.get("type") != "PLANNER_RESPONSE":
            continue
        content = rec.get("content")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                pass
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _text_of(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("text")).strip()
    return ""


def copilot_final_message(transcript_path: str) -> str:
    """Copilot CLI's final reply, from the end of the transcript its agentStop
    hook names. Its record format isn't documented, so this takes the newest
    record that reads as an assistant message — `type` "assistant.message" or
    "assistant", or `role` "assistant" — with its text in `data.content`,
    `message.content` or `content`. Nothing found is an empty message; the
    pane tail still rides with the event."""
    for line in _tail_records(transcript_path):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        kind = str(rec.get("type") or "")
        if kind not in ("assistant.message", "assistant") and rec.get("role") != "assistant":
            continue
        for holder in (rec.get("data"), rec.get("message"), rec):
            if isinstance(holder, dict):
                text = _text_of(holder.get("content"))
                if text:
                    return text
    return ""


# ── Heuristics ───────────────────────────────────────────────────────────────

_TRAILING_MD = re.compile(r"[\s*_`>)\]\"'”’]+$")
# Asks without a question mark: "Say if you'd rather…", "Let me know which…".
# Only at the start of a sentence, so "I'll tell you when it's live" isn't one.
_IMPERATIVE_ASK = re.compile(
    r"^(?:say|tell me|let me know|want me to|shall i|should i|do you want|"
    r"would you like|which (?:one|do you))\b", re.I)


def asks_question(message: str) -> bool:
    """Did the agent end its turn by asking something? Then it is waiting on
    you, not done. Judged on the last paragraph only: a question earlier in a
    report that then carries on is rhetorical, or already answered.

    Within that paragraph, a question counts if it is one of the last two
    sentences ("…should I commit it? Or leave it for review."), or anywhere
    if it is put to you ("Which file would you like next? You can also…").
    "Why did it fail? Because the path was wrong. Fixed and rerun." is not.
    Nor is an ask without a question mark missed: a sentence of the last
    paragraph opening "Say if…", "Let me know…", "Want me to…" counts too."""
    paras = [p for p in re.split(r"\n\s*\n", message or "") if p.strip()]
    if not paras:
        return False
    last = _TRAILING_MD.sub("", paras[-1].strip())
    sentences = [x for x in re.split(r"(?<=[.!?])\s+", last) if x.strip()]
    if any(_IMPERATIVE_ASK.match(x.lstrip("*_ ")) for x in sentences):
        return True
    asks = [i for i, x in enumerate(sentences) if _TRAILING_MD.sub("", x).endswith("?")]
    if not asks:
        return False
    if asks[-1] >= len(sentences) - 2:
        return True
    return any(re.search(r"\byou(r)?\b", sentences[i], re.I) for i in asks)


# ── Reading a pane ───────────────────────────────────────────────────────────
#
# The fallback for what hooks can't see: sessions started before the hooks
# were installed, sessions inside containers (whose agents read their own
# config), and agy holding a question menu — it is still inside its loop then,
# so no Stop fires. Matched against the last lines only, where each TUI keeps
# its footer.

_ASKING = {
    # permission dialogs, plan approval, AskUserQuestion menus
    "claude-code": re.compile(r"Do you want to |Would you like to proceed|"
                              r"Enter to select|^\s*❯\s*\d+\.\s", re.M),
    # ask-user menus ("↑/↓ Navigate · enter Select · esc Skip"), tool
    # approval, and the folder-trust prompt a new workspace starts on
    "antigravity": re.compile(r"enter Select|enter Confirm|esc Skip|"
                              r"Run this command\?|Allow (access|calling)", re.M),
    # Copilot CLI's permission dialog. Unverified against a live session —
    # its hooks are the primary source; tune with [agent.copilot] pane_asking.
    "copilot": re.compile(r"Do you want to |No, and tell Copilot|^\s*❯\s*\d+\.\s", re.M),
}
_BUSY = {
    # "esc to interrupt" — but a narrow window (a phone attached to the
    # session) cuts the footer to "· es…", so any prefix of it counts. And a
    # live token counter on the spinner or on a running subagent's line
    # ("Churning… (3m 45s · ↓ 9.6k tokens", "17m 18s · ↓ 229.2k tokens")
    # means work is underway even when the footer has been cut off entirely;
    # a finished turn says "Worked for 36s · done 2:41 AM", with no counter.
    "claude-code": re.compile(r"esc to interrupt|·\s*e(?:s(?:c(?: (?:t(?:o(?: \w*)?)?)?)?)?)?…\s*$|"
                              r"\d+[hms] · [↓↑] [\d.]+k? tokens", re.M),
    "antigravity": re.compile(r"esc to cancel"),
    "copilot": re.compile(r"(?i)esc to (?:cancel|interrupt)"),
}
_IDLE = {
    "claude-code": re.compile(r"^\s*❯", re.M),
    "antigravity": re.compile(r"\? for shortcuts"),
    "copilot": re.compile(r"@ to mention|/ for commands|for shortcuts"),
}


def set_pane_patterns(agent_type: str, *, asking: str | None = None,
                      busy: str | None = None, idle: str | None = None) -> None:
    """Override (or supply, for an agent not built in) how its pane reads —
    from the config file's [agent.<id>] pane_asking / pane_busy / pane_idle."""
    for table, pattern in ((_ASKING, asking), (_BUSY, busy), (_IDLE, idle)):
        if pattern:
            table[agent_type] = re.compile(pattern, re.M)
_BORDER = re.compile(r"^\s*[─━]{20,}\s*$")
# The agent exited and `exec bash` left a shell in its pane.
_SHELL = re.compile(r"^\S+@\S+:.*[$#]\s*$")


def pane_footer(text: str, lines: int = 14) -> str:
    rows = [r for r in (text or "").splitlines() if r.strip()]
    return "\n".join(rows[-lines:])


def classify_pane(agent_type: str, text: str) -> str | None:
    """asking | busy | idle | shell (the agent exited), or None when the pane
    is something else — still starting, or a TUI we don't know."""
    foot = pane_footer(text)
    if not foot:
        return None
    if _SHELL.match(foot.splitlines()[-1]):
        return "shell"
    if _ASKING.get(agent_type, re.compile(r"$^")).search(foot):
        return "asking"
    if _BUSY.get(agent_type, re.compile(r"$^")).search(foot):
        return "busy"
    if _IDLE.get(agent_type, re.compile(r"$^")).search(foot):
        return "idle"
    return None


def pane_background(text: str) -> int:
    """Background shells the agent left running ("3 shells still running", or
    "· 3 shells ·" in Claude's footer) — what it will wake up for."""
    m = re.findall(r"(\d+) shells?(?: still running| ·)", pane_footer(text))
    return int(m[-1]) if m else 0


_PROMPT_LINE = re.compile(r"^\s*[❯>]\s+\S")
_STATUS_LINE = re.compile(r"^\s*(✻ \w+ for |✔ Update installed|\? for shortcuts)")


def pane_body(text: str, keep: int = 40) -> str:
    """What the agent wrote this turn: the lines after the last prompt the
    user submitted (❯ in Claude Code, > in agy) and above the input box, minus
    the "Worked for…" line and banners. Without the cut, scrollback carries
    the previous turn — its question, and the answer already given to it."""
    rows = (text or "").rstrip().splitlines()
    borders = [i for i, r in enumerate(rows) if _BORDER.match(r)]
    if len(borders) >= 2:
        rows = rows[:borders[-2]]
    prompts = [i for i, r in enumerate(rows) if _PROMPT_LINE.match(r)]
    if prompts:
        rows = rows[prompts[-1] + 1:]
    rows = [r.rstrip() for r in rows if not _STATUS_LINE.match(r)]
    while rows and not rows[-1].strip():
        rows.pop()
    return "\n".join(rows[-keep:]).strip()


# ── The state machine ────────────────────────────────────────────────────────

def transition(state: str | None, event: str, info: dict) -> str | None:
    """The state an event moves an instance to, or None to leave it be.

    Events (normalized from every source):
      session_start  info: source = startup | resume | clear | compact
      prompt         a prompt was submitted
      stop           the turn ended; info: message, reason
      stop_error     the turn ended on an error
      cancelled      the user cancelled the turn
      notify         info: kind = permission_prompt | idle_prompt | ...
      asking         the pane shows a question menu or permission dialog
      busy           the pane shows the agent working
      settled        the pane shows the agent back at its prompt
      menu_closed    the pane's question menu went away without work resuming
      exited         the pane shows a shell: the agent is gone
      revived        an agent is back in a pane we had marked offline
      dismiss        the user looked at it
      session_end    the agent exited
    """
    if event == "session_start":
        src = info.get("source")
        if src == "compact":
            return None                      # compaction happens mid-work too
        if src == "clear":
            return "idle"
        return "starting"
    if event == "prompt":
        return "working"
    if event == "stop":
        return "needs_input" if asks_question(info.get("message", "")) else "completed"
    if event == "stop_error":
        return "error"
    if event == "cancelled":
        return "idle"
    if event == "notify":
        kind = info.get("kind")
        if kind in ("permission_prompt", "elicitation_dialog"):
            return "needs_input"
        if kind == "idle_prompt" and state in ("working", "starting"):
            # Waiting on you for a minute while we still think it's busy:
            # a Stop went missing. It finished.
            return "completed"
        return None
    if event == "asking":
        return None if state == "needs_input" else "needs_input"
    if event == "busy":
        return None if state == "working" else "working"
    if event == "settled":
        if state == "working":
            return "needs_input" if asks_question(info.get("message", "")) else "completed"
        return None
    if event == "menu_closed":
        return "idle" if state == "needs_input" else None
    if event == "exited":
        return None if state == "offline" else "offline"
    if event == "revived":
        return "idle" if state == "offline" else None
    if event == "dismiss":
        return "idle" if state in DISMISSABLE else None
    if event == "session_end":
        return "offline"
    return None


# ── Writing ──────────────────────────────────────────────────────────────────

def resolve_session(db: sqlite3.Connection, session: str | None) -> tuple:
    """(project, instance, agent_type) for a tmux session, as the server last
    recorded it — or Nones, and the server fills them in when it reads."""
    if not session:
        return None, None, None
    row = db.execute("SELECT project, instance, agent_type FROM sessions "
                     "WHERE session = ?", (session,)).fetchone()
    return (row["project"], row["instance"], row["agent_type"]) if row else (None, None, None)


def record(db: sqlite3.Connection, *, event: str, source: str,
           project: str | None, instance: str | None, agent_type: str | None,
           session: str | None = None, raw_event: str | None = None,
           message: str | None = None, transcript_path: str | None = None,
           pane: str | None = None, agent_session: str | None = None,
           cwd: str | None = None, detail: dict | None = None,
           ts: float | None = None) -> dict:
    """Log one event and move the instance's state. Returns the event id and
    the instance's state afterwards. One transaction, so a concurrent reader
    never sees the event without its effect."""
    ts = ts or time.time()
    detail = detail or {}
    if pane:
        pane = "\n".join(pane.rstrip("\n").splitlines()[-PANE_KEEP:])
    db.execute("BEGIN IMMEDIATE")
    try:
        cur = None
        if project and instance:
            cur = db.execute("SELECT * FROM instances WHERE project = ? AND instance = ?",
                             (project, instance)).fetchone()
        old = cur["state"] if cur else None
        new = transition(old, event, {**detail, "message": message or ""})
        eid = db.execute(
            "INSERT INTO events (ts, project, instance, agent_type, session, event, "
            "raw_event, source, state, message, transcript_path, pane, agent_session, "
            "cwd, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, project, instance, agent_type, session, event, raw_event, source,
             new, message, transcript_path, pane, agent_session, cwd,
             json.dumps(detail) if detail else None)).lastrowid
        state = new or old
        if project and instance:
            badges = json.loads(cur["detail"]) if cur and cur["detail"] else {}
            if "background_tasks" in detail:
                badges["background_tasks"] = detail["background_tasks"]
            if cur is None:
                db.execute(
                    "INSERT INTO instances (project, instance, agent_type, session, state, "
                    "since, updated, last_event, last_hook, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (project, instance, agent_type, session, new or "idle", ts, ts,
                     eid, ts if source == "hook" else None, json.dumps(badges)))
                state = new or "idle"
            else:
                changed = new is not None and new != old
                db.execute(
                    "UPDATE instances SET agent_type = COALESCE(?, agent_type), "
                    "session = COALESCE(?, session), state = ?, since = ?, updated = ?, "
                    "last_event = ?, last_hook = ?, detail = ? "
                    "WHERE project = ? AND instance = ?",
                    (agent_type, session, state, ts if changed else cur["since"], ts,
                     eid if new is not None else cur["last_event"],
                     ts if source == "hook" else cur["last_hook"],
                     json.dumps(badges), project, instance))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return {"id": eid, "state": state, "changed": new is not None and new != old}


def sync_sessions(db: sqlite3.Connection, rows: list[tuple]) -> None:
    """Replace the session map: rows of (session, project, instance, agent_type)."""
    now = time.time()
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("DELETE FROM sessions")
        db.executemany("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?)",
                       [(*r, now) for r in rows])
        # Events a hook logged before the server ever mapped its session.
        db.execute("""UPDATE events SET project = s.project, instance = s.instance,
                      agent_type = s.agent_type FROM sessions s
                      WHERE events.project IS NULL AND events.session = s.session""")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def prune(db: sqlite3.Connection, days: int = RETENTION_DAYS) -> None:
    db.execute("DELETE FROM events WHERE ts < ? AND id NOT IN "
               "(SELECT last_event FROM instances WHERE last_event IS NOT NULL)",
               (time.time() - days * 86400,))
    db.execute("DELETE FROM summaries WHERE event_id NOT IN (SELECT id FROM events)")


def turn_text(row) -> str:
    """The agent's final message for an event row. A pane-detected turn is
    re-read from its stored pane, so it gets the current cut (pane_body)
    rather than whatever cut was in force when it was recorded."""
    if row["source"] == "pane" and row["pane"]:
        return pane_body(row["pane"])
    return row["message"] or ""


def save_summary(db: sqlite3.Connection, event_id: int, source: str, *,
                 model: str | None = None, status: str | None = None,
                 summary: str | None = None, ms: int | None = None,
                 error: str | None = None) -> None:
    db.execute("INSERT OR REPLACE INTO summaries VALUES (?,?,?,?,?,?,?,?)",
               (event_id, source, time.time(), model, status, summary, ms, error))


def summary_for(db: sqlite3.Connection, event_id: int) -> dict | None:
    """The best summary of an event: Sonnet's if one was asked for, else the
    local model's. Errors count as no summary."""
    rows = {r["source"]: r for r in db.execute(
        "SELECT * FROM summaries WHERE event_id = ? AND error IS NULL", (event_id,))}
    row = rows.get("sonnet") or rows.get("granite")
    if not row:
        return None
    return {"text": row["summary"], "status_guess": row["status"],
            "source": row["source"], "model": row["model"], "ts": row["ts"]}

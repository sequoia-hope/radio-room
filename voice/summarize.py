"""Rough local summaries of an agent's turn, from a small model on this
machine (Granite via Ollama) — so the overview reads in one line per agent and
keeps working with no cloud credits at all.

One narrow job: from the agent's final message and the tail of its pane, a
status guess and a one-line summary, as JSON. Nothing else is asked of the
model, so a small one is enough, and its output is always shown labelled as a
rough machine summary next to a link to the full message.

Stdlib only.
"""

import json
import re
import time
import urllib.error
import urllib.request

STATUSES = ["completed", "needs_input", "error"]

# Two yes/no questions rather than a pick from four states: a small model
# asked to choose a label leans hard on "needs_input" (measured on 9 real
# turns: 7 wrong), but answers narrow questions about the last paragraph
# well. The status is derived from the answers.
SCHEMA = {
    "type": "object",
    "properties": {
        "asks_user": {"type": "boolean"},
        "failed": {"type": "boolean"},
        "summary": {"type": "string"},
    },
    "required": ["asks_user", "failed", "summary"],
}

SYSTEM = """\
You read the end of a coding agent's turn and label it for a dashboard.

Reply with JSON only: {"asks_user": ..., "failed": ..., "summary": ...}

asks_user: true only if the LAST PARAGRAPH of the agent's message puts a
question or a choice to the user right now and waits for the answer. False if
it only reports what it did, says what it will do next, says it will ask
later, or says it is waiting for jobs or builds to finish.

failed: true only if the agent says it failed, is blocked, or hit an error it
could not fix.

summary: ONE short line, at most 16 words, plain text, no markdown, no quotes.
What the agent did this turn; if asks_user is true, end with what it asks.
Write it as a status line ("Fixed the login bug; asks whether to commit"). Use
only facts in the message. Never mention a question unless asks_user is true."""

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
BOX = re.compile(r"^[\s─━│┃╭╮╰╯┌┐└┘├┤┬┴┼⎿▀▄█▌▐░▒▓]*$")
# The agents' own chrome, which a reader of the pane would otherwise take for
# content ("? for shortcuts" read as the agent asking about shortcuts).
CHROME = re.compile(r"^\s*(\? for shortcuts|⏵⏵|✔ Update installed|✻ \w+ for |"
                    r"[❯>]\s*$|esc to |\(shift\+tab|● main|◯ )")


def clean_pane(text: str, lines: int = 20) -> str:
    """The pane as plain text: no escapes, no rules, no TUI chrome, no input
    box — just what the agent printed."""
    rows = [ANSI.sub("", r).rstrip() for r in (text or "").splitlines()]
    rows = [r for r in rows if r.strip() and not BOX.match(r) and not CHROME.match(r)]
    return "\n".join(rows[-lines:])


def clip_message(message: str, head: int = 400, tail: int = 2600) -> str:
    """Long reports keep their opening and their end, where the outcome and
    any question to the user live."""
    message = (message or "").strip()
    if len(message) <= head + tail + 20:
        return message
    return message[:head].rstrip() + "\n[…]\n" + message[-tail:].lstrip()


def last_paragraph(message: str) -> str:
    paras = [p.strip() for p in re.split(r"\n\s*\n", message or "") if p.strip()]
    return paras[-1] if paras else ""


def build_prompt(project: str, agent: str, message: str, pane: str) -> str:
    """Kept short on purpose — it runs on the CPU, where prompt length is most
    of the cost: about 1,000 characters, against the 4,000 it used to be."""
    parts = [f"Project: {project}\nAgent: {agent}"]
    if message:
        parts.append(f"The agent's final message:\n<message>\n"
                     f"{clip_message(message, 150, 700)}\n</message>")
        last = last_paragraph(message)
        if len(last) > 400:
            last = "[…]" + last[-400:].lstrip()
        parts.append(f"Its last paragraph:\n<last_paragraph>\n{last}\n</last_paragraph>")
    pane = "\n".join(r[:120] for r in clean_pane(pane, 6).splitlines())
    if pane:
        parts.append(f"End of its terminal:\n<terminal>\n{pane}\n</terminal>")
    return "\n\n".join(parts)


class SummaryError(RuntimeError):
    pass


def summarize(cfg: dict, project: str, agent: str, message: str, pane: str) -> dict:
    """{status, summary, model, ms}. Raises SummaryError when the model is
    unreachable or answers with something unusable."""
    base = str(cfg["summary_ollama_url"]).rstrip("/")
    body = {
        "model": cfg["summary_local_model"],
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": build_prompt(project, agent, message, pane)}],
        "stream": False,
        "format": SCHEMA,
        "think": False,
        "keep_alive": cfg.get("summary_keep_alive", "30m"),
        "options": {
            # The prompt is short now; a smaller context is less to allocate.
            "num_ctx": int(cfg.get("summary_num_ctx", 2048)),
            "temperature": 0.1,
            "num_predict": 120,
        },
    }
    # 0 keeps the model off the GPU entirely; unset lets Ollama offload layers.
    if cfg.get("summary_num_gpu") is not None and int(cfg["summary_num_gpu"]) >= 0:
        body["options"]["num_gpu"] = int(cfg["summary_num_gpu"])
    req = urllib.request.Request(f"{base}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=float(cfg.get("summary_local_timeout", 120))) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read()[:200].decode("utf-8", "replace").strip()
        raise SummaryError(f"ollama HTTP {e.code}: {detail}")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        raise SummaryError(f"can't reach ollama at {base}: {e}")
    ms = int((time.monotonic() - t0) * 1000)
    try:
        out = json.loads((data.get("message") or {}).get("content") or "")
    except json.JSONDecodeError:
        raise SummaryError("model returned something that isn't JSON")
    summary = " ".join(str(out.get("summary") or "").split()).strip(" \"'")
    if not summary or not isinstance(out.get("asks_user"), bool):
        raise SummaryError(f"model returned an unusable answer: {out!r}")
    status = ("error" if out.get("failed") else
              "needs_input" if out["asks_user"] else "completed")
    return {"status": status, "summary": summary[:200],
            "model": body["model"], "ms": ms}

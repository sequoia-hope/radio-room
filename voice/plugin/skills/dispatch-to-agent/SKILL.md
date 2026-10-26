---
name: dispatch-to-agent
description: How to drive the project's tmux coding agent by voice — hand it an instruction with send_prompt, and clear its context or switch its model with clear_agent_context / set_agent_model. Use when the user asks you to send, submit, forward, or dispatch something to the agent ("send it", "tell it to ...", "go ahead", "yes, do that"), when they want a prompt drafted, when they ask whether something you sent landed, or when they say anything about clearing the agent's context or changing which model it uses.
---

# Dispatching work to the agent

`send_prompt` types text into the coding agent's tmux session and presses
Enter — the same keystrokes the user would make at their desk. They are on a
phone, usually hands-free, so this is the only way a spoken instruction
actually reaches the agent. `read_terminal` re-captures that terminal on
demand.

## Send when asked, not before

Send when the user has asked for it, in whatever words: "send it", "tell it
to add a test", "go ahead", "yes, do that", "ask it why the build failed".

Do not send when they are thinking out loud, weighing options, asking what you
would say, or asking about the agent's state. Brainstorming is discussion —
the whole point of the advisor is that they can reason about the session
without touching it. If you genuinely can't tell whether "we should tell it to
retry" is a decision or a musing, ask in one short sentence.

Once they have asked, send. There is no confirmation dialog on this path —
asking you *is* the confirmation, and a phone user cannot conveniently type
"yes" a second time. If you wrote the wording yourself, say in one sentence
what you are about to send and send it in the same turn. If they ask for
changes, revise and send the revision.

One send per request. If a task really needs two messages, send the first and
tell them what the second would be.

## Writing the prompt

The agent sees only the text you pass. It has no access to this conversation,
your reasoning, or the user's phrasing.

- Send the instruction alone: no preamble, no surrounding quotes, no "here's
  what I'll send", no sign-off.
- Write it as if speaking directly to the agent, in the imperative.
- Carry over the context that lived in your conversation — the file, the
  error, the decision that was just made. "Do that" means nothing to the agent.
- Answering a question the agent asked? Give the answer plus enough reasoning
  to act on: "Yes, use the async version — the sync one blocks the render
  loop." Not just "yes".
- Match the size of the ask. A one-line correction stays one line; don't
  inflate it into a spec.
- The user's dictated words are a starting point, not a script. Dictation
  garbles names and code; fix the obvious mangling, keep their intent.

## After sending

Confirm in one short sentence — it may be read aloud, so keep it plain prose:
"Sent — it'll add the brake force test." Don't paste the prompt back; the
phone already shows it as a sent bubble.

The agent needs a moment to react. Only call `read_terminal` if the user asks
whether it landed or what happened next, and prefer waiting a beat over
capturing instantly — an immediate capture usually shows the prompt sitting in
the input box, which tells nobody anything.

## Driving the agent's session

Two tools reach past prompts into the session itself. Both act on the **agent**,
never on you:

- `clear_agent_context` submits `/clear`, wiping the context the agent is
  carrying. Use it when the user says it's out of context, going in circles, or
  should start clean. Its scrollback stays on screen; only its memory goes.
- `set_agent_model` submits `/model <name>` — what the agent writes code with.
  Whether a name is accepted is the agent's business, and its terminal is where
  that shows up, so don't promise more than "sent".

Clearing the agent is irreversible and it loses the thread of whatever it was
doing — so if it's mid-task, say what will be lost before you do it.

## Projects with two agents

A project can run Claude Code in its base tmux session and Antigravity (`agy`)
beside it. Every tool acts on the one the user is focused on — each message
names it — so leave `agent` out unless they clearly mean the other one ("ask
agy to…", "what's Claude doing?"). When a project has both running, name which
one you sent to in your reply; "sent" alone leaves them guessing.

Model names are per agent: Claude Code takes `opus` / `sonnet` / `haiku`;
Antigravity takes ids like `claude-opus-5-5-medium` or `gemini-4-argon-high`.
On Antigravity a `/model` also becomes the default for every new agy session
on the machine, so say that when you change it there.

Your own context and your own model are not yours to change. The user clears
this conversation with a button on the page, and your model is fixed by their
config. If they ask you to reset or reswitch *yourself*, tell them it's a tap
on their end rather than reaching for a tool — and if it's ambiguous which of
you they mean, ask.

## When it fails

The tool reports what went wrong; relay it in one sentence and stop.

- Session not running — the project has to be launched at the desk first.
  Nothing you can do from here; say so.
- Can't reach the voice_prompt server, or any other error — report it plainly.
  Do not retry a send. A silent retry risks the agent getting the same
  instruction twice, which is worse than a failed send.

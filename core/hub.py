#!/usr/bin/env python3
"""Project hub — one page listing every project's web instance.

    ./hub.py            serve on the port from the registry's [hub] table
    ./hub.py --port N   override

The page is static; all data arrives from /api/status, which re-probes every
registered port on each request. Links are assembled in the browser from
window.location.hostname, so the same page works whether it was opened at
localhost, the LAN address, or the Tailscale name — except for services that
bind one specific IP, which carry an explicit host in the registry.

Endpoints:
    GET  /              dashboard
    GET  /api/status    JSON snapshot of every project
    POST /api/start     {"name": ...} — bring a managed web instance up
    POST /api/stop      {"name": ...} — kill its tmux session
"""

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import listen
import registry

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Projects</title>
<style>
:root {
  --bg: #f6f7f9; --card: #ffffff; --line: #e2e5ea; --fg: #14171c;
  --muted: #5f6875; --accent: #2f6feb; --up: #1a9c5b; --down: #98a1ae;
  --chip: #eef1f5; --shadow: 0 1px 2px rgba(16,20,28,.06), 0 4px 12px rgba(16,20,28,.05);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0e1116; --card: #171b22; --line: #262c36; --fg: #e6e9ee;
    --muted: #8b94a3; --accent: #5b9bff; --up: #35c07d; --down: #4d5563;
    --chip: #222833; --shadow: none;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  padding: 0 0 3rem;
}
header {
  padding: 1.5rem 1.25rem 1rem; max-width: 1100px; margin: 0 auto;
  display: flex; align-items: baseline; gap: .75rem; flex-wrap: wrap;
}
h1 { font-size: 1.4rem; margin: 0; letter-spacing: -.01em; }
.sub { color: var(--muted); font-size: .85rem; }
.sub b { color: var(--fg); font-weight: 600; }
main {
  max-width: 1100px; margin: 0 auto; padding: 0 1.25rem;
  display: grid; gap: .9rem;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
}
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: .95rem 1rem 1rem; box-shadow: var(--shadow);
  display: flex; flex-direction: column; gap: .55rem;
}
.card.noweb { opacity: .62; }
.top { display: flex; align-items: center; gap: .5rem; }
.dot { width: 9px; height: 9px; border-radius: 50%; background: var(--down); flex: none; }
.dot.up { background: var(--up); box-shadow: 0 0 0 3px color-mix(in srgb, var(--up) 22%, transparent); }
.name { font-weight: 620; font-size: 1.02rem; letter-spacing: -.01em; }
.desc { color: var(--muted); font-size: .85rem; }
.links { display: flex; flex-wrap: wrap; gap: .4rem; }
a.link {
  display: inline-flex; align-items: baseline; gap: .4rem; text-decoration: none;
  background: var(--accent); color: #fff; padding: .4rem .7rem;
  border-radius: 7px; font-size: .875rem; font-weight: 550;
}
a.link.alt { background: var(--chip); color: var(--fg); border: 1px solid var(--line); }
a.link.dead { background: var(--chip); color: var(--muted); border: 1px solid var(--line); }
a.link .port { font-variant-numeric: tabular-nums; opacity: .75; font-weight: 450; font-size: .8rem; }
.meta { display: flex; flex-wrap: wrap; gap: .35rem; margin-top: auto; padding-top: .25rem; }
.chip {
  background: var(--chip); border: 1px solid var(--line); color: var(--muted);
  border-radius: 999px; padding: .15rem .55rem; font-size: .74rem;
  font-variant-numeric: tabular-nums;
}
.chip.on { color: var(--up); border-color: color-mix(in srgb, var(--up) 35%, var(--line)); }
.actions { display: flex; gap: .4rem; }
button {
  font: inherit; font-size: .82rem; font-weight: 550; cursor: pointer;
  background: var(--chip); color: var(--fg); border: 1px solid var(--line);
  border-radius: 7px; padding: .32rem .7rem;
}
button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
button:disabled { opacity: .45; cursor: default; }
footer {
  max-width: 1100px; margin: 1.75rem auto 0; padding: 0 1.25rem;
  color: var(--muted); font-size: .8rem;
}
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .95em; }
#toast {
  position: fixed; left: 50%; bottom: 1.25rem; transform: translateX(-50%);
  background: var(--card); border: 1px solid var(--line); box-shadow: var(--shadow);
  padding: .5rem .9rem; border-radius: 8px; font-size: .85rem;
  opacity: 0; transition: opacity .2s; pointer-events: none; max-width: 90vw;
}
#toast.show { opacity: 1; }
</style>
</head>
<body>
<header>
  <h1>Projects</h1>
  <div class="sub" id="summary">loading…</div>
</header>
<main id="grid"></main>
<footer>
  Add a project with
  <code>proj add &lt;name&gt; &lt;dir&gt;</code>, which allocates the next free port.
</footer>
<div id="toast"></div>
<script>
const grid = document.getElementById('grid');
const summary = document.getElementById('summary');
const toastEl = document.getElementById('toast');
let toastTimer;

function toast(msg) {
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 3200);
}

// Services that bind 0.0.0.0 are reachable at whatever host this page was
// opened as, which keeps links working from a phone over Tailscale. Only a
// service pinned to one IP overrides it.
function urlFor(host, port, path, scheme) {
  return (scheme || 'http') + '://' + (host || location.hostname) + ':' + port + (path || '/');
}

/* Label the primary link with the port it actually opens — a proxied web_url
   usually leaves that implicit (443), so fill it in from the scheme. */
function linkPort(p) {
  if (!p.url) return p.port;
  try {
    const u = new URL(p.url);
    return u.port || (u.protocol === 'https:' ? 443 : 80);
  } catch (e) { return p.port; }
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function card(p) {
  const c = el('div', 'card' + (p.port ? '' : ' noweb'));

  const top = el('div', 'top');
  top.append(el('span', 'dot' + (p.up ? ' up' : '')), el('span', 'name', p.name));
  c.append(top);

  if (p.desc) c.append(el('div', 'desc', p.desc));

  if (p.port) {
    const links = el('div', 'links');
    const a = el('a', 'link' + (p.up ? '' : ' dead'));
    // web_url wins when a proxy fronts the project: the port stays the app's
    // own, so the dot beside it still reports the app rather than the proxy.
    a.href = p.url || urlFor(p.host, p.port, p.path, p.scheme);
    a.target = '_blank';
    a.rel = 'noopener';
    a.append(el('span', null, 'Open'), el('span', 'port', linkPort(p)));
    links.append(a);
    for (const e of p.extra) {
      const b = el('a', 'link ' + (e.up ? 'alt' : 'dead'));
      b.href = urlFor(e.host, e.port, e.path, e.scheme);
      b.target = '_blank';
      b.rel = 'noopener';
      b.append(el('span', null, e.name), el('span', 'port', e.port));
      links.append(b);
    }
    c.append(links);
  }

  const meta = el('div', 'meta');
  if (p.port) meta.append(el('span', 'chip', p.fixed ? 'port: adopted' : 'port: allocated'));
  for (const [id, a] of Object.entries(p.agents || {})) {
    if (!a.session) continue;
    meta.append(el('span', 'chip' + (a.up ? ' on' : ''),
      (a.kind === 'docker' ? 'docker ' : '') + id + ' ' + a.session +
      (a.up ? ' ·live' : '')));
  }
  if (p.supervised) meta.append(el('span', 'chip', 'external supervisor'));
  if (!p.port) meta.append(el('span', 'chip', 'no web instance'));
  c.append(meta);

  if (p.managed) {
    const actions = el('div', 'actions');
    const start = el('button', null, p.up ? 'Restart' : 'Start');
    const stop = el('button', null, 'Stop');
    // Stoppable if the tmux session is up, or if something is holding the port
    // (a start script that daemonised away from its pane).
    stop.disabled = !p.web_session_up && !p.up;
    start.onclick = () => act(p.up ? 'restart' : 'start', p.name, start, stop);
    stop.onclick = () => act('stop', p.name, start, stop);
    actions.append(start, stop);
    c.append(actions);
  }
  return c;
}

async function act(what, name, ...btns) {
  btns.forEach(b => b.disabled = true);
  try {
    if (what === 'restart') {
      await fetch('/api/stop', {method: 'POST', body: JSON.stringify({name})});
    }
    const endpoint = what === 'stop' ? '/api/stop' : '/api/start';
    const r = await fetch(endpoint, {method: 'POST', body: JSON.stringify({name})});
    const j = await r.json();
    toast(name + ': ' + j.message);
  } catch (e) {
    toast(name + ': ' + e);
  }
  // A server needs a moment to bind before the next probe sees it.
  setTimeout(refresh, 900);
}

async function refresh() {
  let data;
  try {
    data = await (await fetch('/api/status')).json();
  } catch (e) {
    summary.textContent = 'hub unreachable';
    return;
  }
  const withWeb = data.projects.filter(p => p.port);
  const live = withWeb.filter(p => p.up).length;
  summary.innerHTML = '<b>' + live + '</b> of <b>' + withWeb.length +
    '</b> web instances up · <b>' + data.projects.length + '</b> projects · pool ' +
    data.pool[0] + '–' + data.pool[1] + ', next free <b>' + data.next_port + '</b>';
  grid.replaceChildren(...data.projects.map(card));
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "projecthub/1.0"

    def log_message(self, fmt, *args):
        # Status polls every 5s from every open tab would bury anything useful.
        if "/api/status" not in (args[0] if args else ""):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype):
        payload = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/status":
            cfg = registry.load()
            try:
                next_port = registry.allocate_port(cfg)
            except RuntimeError:
                next_port = None
            self._json({
                "projects": registry.status(cfg),
                "pool": cfg["hub"]["pool"],
                "next_port": next_port,
            })
        elif path == "/healthz":
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/start", "/api/stop"):
            self._send(404, "not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            name = json.loads(self.rfile.read(length) or b"{}").get("name")
        except (ValueError, json.JSONDecodeError):
            self._json({"ok": False, "message": "bad request body"}, 400)
            return

        cfg = registry.load()
        project = registry.find(cfg, name or "")
        if project is None:
            self._json({"ok": False, "message": f"unknown project '{name}'"}, 404)
            return

        action = registry.start_web if path == "/api/start" else registry.stop_web
        ok, message = action(project)
        self._json({"ok": ok, "message": message}, 200 if ok else 500)


def main():
    cfg = registry.load()
    ap = argparse.ArgumentParser(description="Serve the project hub dashboard.")
    ap.add_argument("--port", type=int, default=cfg["hub"]["port"])
    ap.add_argument("--bind", default=cfg["hub"]["bind"],
                    help="local (the default), tailscale, any, or an address; "
                         "start/stop are unauthenticated")
    args = ap.parse_args()

    try:
        addrs = listen.addresses(args.bind)
    except listen.BindError as e:
        sys.exit(f"hub: {e}")
    servers = [ThreadingHTTPServer((addr, args.port), Handler) for addr in addrs]
    print(f"project hub → port {args.port} on {', '.join(addrs)}  "
          f"({len(cfg['projects'])} projects from {registry.CONFIG_PATH})")
    if listen.exposed(addrs):
        print("hub: start/stop are open to everyone on that network", file=sys.stderr)
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()


if __name__ == "__main__":
    main()

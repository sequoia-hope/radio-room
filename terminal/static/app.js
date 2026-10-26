/* The browser half of the terminal.
 *
 * Three things are load-bearing here, and the rest is wiring:
 *
 *  1. The document never scrolls. The only scrollable thing on the page is
 *     xterm's own viewport, so a wheel or a drag can only ever move the
 *     terminal — it can never grab the page.
 *  2. Layout comes from visualViewport, not from the window. When the soft
 *     keyboard opens, the terminal gets shorter; it does not get pushed up
 *     under the keyboard and it does not leave the page scrolled.
 *  3. The connection is disposable. The PTY lives on the server, so dropping
 *     and reattaching is routine rather than an error, and reattaching
 *     rebuilds the screen from replayed history.
 */

'use strict';

const MIN_FONT = 8;
const MAX_FONT = 28;
const FONT_KEY = 'ssh_client.fontSize';

const el = {
  app: document.getElementById('app'),
  term: document.getElementById('term'),
  keys: document.getElementById('keys'),
  tabs: document.getElementById('tabs'),
  tablist: document.getElementById('tablist'),
  newtab: document.getElementById('newtab'),
  mic: document.getElementById('mic'),
  veil: document.getElementById('veil'),
  veilText: document.getElementById('veil-text'),
  paste: document.getElementById('paste'),
  pasteField: document.getElementById('paste-field'),
  pasteCancel: document.getElementById('paste-cancel'),
  pasteImage: document.getElementById('paste-image'),
};

// A pointer media query alone is unreliable — some desktop browsers report a
// coarse pointer with no touchscreen at all. Require both an actual touch
// digitizer and the absence of a hover-capable pointer.
const isTouch =
  (navigator.maxTouchPoints > 0 || 'ontouchstart' in window) &&
  window.matchMedia('(hover: none)').matches;

// ── terminal ────────────────────────────────────────────────────────────────

function storedFont() {
  const saved = parseFloat(localStorage.getItem(FONT_KEY));
  if (saved >= MIN_FONT && saved <= MAX_FONT) return saved;
  // Small enough that the launcher and claude get the width they are laid out
  // for, on a phone in portrait as much as on a desktop.
  return 10;
}

const term = new Terminal({
  fontSize: storedFont(),
  fontFamily: '"SF Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, "DejaVu Sans Mono", monospace',
  lineHeight: 1.15,
  letterSpacing: 0,
  cursorBlink: true,
  cursorStyle: 'block',
  scrollback: 10000,
  // One wheel notch moving one line makes a long build feel like it will never
  // end; three is what a native terminal does.
  scrollSensitivity: 3,
  fastScrollSensitivity: 10,
  macOptionIsMeta: true,
  allowProposedApi: true,   // required by the unicode11 addon
  allowTransparency: false,
  theme: {
    background: '#0b0e14',
    foreground: '#d5dae2',
    cursor: '#7fd88f',
    cursorAccent: '#0b0e14',
    selectionBackground: '#2b3646',
    black: '#0b0e14',   brightBlack: '#5c6675',
    red: '#f06c75',     brightRed: '#ff8b91',
    green: '#7fd88f',   brightGreen: '#9ff0ad',
    yellow: '#e3c07b',  brightYellow: '#f5d99a',
    blue: '#61afef',    brightBlue: '#8ac6ff',
    magenta: '#c678dd', brightMagenta: '#e0a0f0',
    cyan: '#56b6c2',    brightCyan: '#7fd3de',
    white: '#d5dae2',   brightWhite: '#ffffff',
  },
});

const fit = new FitAddon.FitAddon();
term.loadAddon(fit);
term.loadAddon(new WebLinksAddon.WebLinksAddon());

// Claude Code and the launcher both draw with emoji and box-drawing runs, and
// getting their widths wrong shifts every column after them.
try {
  const unicode11 = new Unicode11Addon.Unicode11Addon();
  term.loadAddon(unicode11);
  term.unicode.activeVersion = '11';
} catch (e) {
  console.warn('unicode11 addon unavailable', e);
}

term.open(el.term);

// The WebGL renderer is what keeps a fast-scrolling build smooth. It is also
// the thing most likely to be missing or to lose its context, so every path
// here has to end with a working terminal.
try {
  const webgl = new WebglAddon.WebglAddon();
  webgl.onContextLoss(() => webgl.dispose());
  term.loadAddon(webgl);
} catch (e) {
  console.warn('webgl renderer unavailable, falling back', e);
}

term.focus();

// ── talking to the server ───────────────────────────────────────────────────

// Declared up here because the first relayout() happens during load, and it
// reports the terminal's size through send().
let ws = null;
let retry = 0;
let reconnectTimer = null;
let exited = false;

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

// ── layout ──────────────────────────────────────────────────────────────────

const vv = window.visualViewport;
let lastSize = { cols: 0, rows: 0 };

function relayout() {
  // iOS sometimes scrolls the document to reveal the focused element before
  // firing the resize; undo that or the top of the terminal is cut off.
  if (window.scrollY !== 0 || window.scrollX !== 0) window.scrollTo(0, 0);

  const height = vv ? vv.height : window.innerHeight;
  el.app.style.height = height + 'px';
  el.app.style.transform = vv && vv.offsetTop ? `translateY(${vv.offsetTop}px)` : '';

  try {
    fit.fit();
  } catch (e) {
    return; // element not laid out yet; the next event will catch it
  }
  if (term.cols !== lastSize.cols || term.rows !== lastSize.rows) {
    lastSize = { cols: term.cols, rows: term.rows };
    send({ t: 'resize', cols: term.cols, rows: term.rows });
  }
}

// rAF-coalesced: the keyboard animating open fires resize on every frame.
let pending = false;
function scheduleRelayout() {
  if (pending) return;
  pending = true;
  requestAnimationFrame(() => { pending = false; relayout(); });
}

window.addEventListener('resize', scheduleRelayout);
window.addEventListener('orientationchange', () => setTimeout(relayout, 120));
if (vv) {
  vv.addEventListener('resize', scheduleRelayout);
  vv.addEventListener('scroll', scheduleRelayout);
}
// Web fonts and the WebGL renderer both settle after first paint, and the cell
// size they settle on is what the fit depends on.
if (document.fonts && document.fonts.ready) document.fonts.ready.then(relayout);
relayout();

// ── font size ───────────────────────────────────────────────────────────────

function setFont(size) {
  const next = Math.max(MIN_FONT, Math.min(MAX_FONT, size));
  if (Math.abs(next - term.options.fontSize) < 0.01) return;
  term.options.fontSize = next;
  localStorage.setItem(FONT_KEY, String(Math.round(next * 10) / 10));
  relayout();
}

el.term.addEventListener('wheel', (e) => {
  if (!e.ctrlKey && !e.metaKey) return;   // plain wheel belongs to the terminal
  e.preventDefault();
  setFont(term.options.fontSize + (e.deltaY < 0 ? 1 : -1));
}, { passive: false });

// ── touch: scrolling and pinch ──────────────────────────────────────────────

// xterm 6 draws the screen into a canvas and moves the view itself; its
// viewport is not a scrollable box, so there is nothing for the browser to pan
// and a drag over the terminal does nothing on its own. Hence `touch-action:
// none` in the stylesheet and the whole gesture handled here — which is also
// what makes it possible to give the flick a glide instead of stopping dead.

const FRICTION = 0.94;        // per frame; ~0.5s of glide from a firm flick
const GLIDE_FLOOR = 0.02;     // px/ms below which the glide is over
const FLICK_MIN = 0.15;       // px/ms — slower than this was a drag, not a flick
const FLICK_PAUSE = 80;       // ms — held this still before lifting, no flick

let drag = null;
let lastGestureWasScroll = false;
let residue = 0;              // sub-cell pixels carried between events
let pointer = { x: 0, y: 0 }; // where the gesture is, for mouse reports
let glideVelocity = 0;
let glideFrame = 0;

function cellHeight() {
  const screen = el.term.querySelector('.xterm-screen');
  return screen ? screen.getBoundingClientRect().height / term.rows : 0;
}

function scrollLines(lines) {
  if (term.modes.mouseTrackingMode !== 'none') {
    // Something on the far side asked for the mouse — tmux with `mouse on`,
    // or claude, which scrolls its own transcript. Hand it a wheel event and
    // let xterm encode it: xterm knows whether the app negotiated SGR, urxvt
    // or the original X10 form, and that knowledge belongs in one place.
    // Sending cursor keys here instead is what makes a scroll walk through a
    // menu rather than scroll anything.
    const cell = cellHeight() || 16;
    term.element.dispatchEvent(new WheelEvent('wheel', {
      deltaY: lines * cell,
      deltaMode: 0,
      clientX: pointer.x,
      clientY: pointer.y,
      bubbles: true,
      cancelable: true,
    }));
  } else if (term.buffer.active.type === 'alternate') {
    // A full-screen app that does not want the mouse. There is no scrollback
    // to move, so match what a wheel does here: cursor keys. Capped so a hard
    // flick cannot fire off fifty keypresses.
    const key = arrow(lines < 0 ? 'A' : 'B');
    send({ t: 'i', d: key.repeat(Math.min(8, Math.abs(lines))) });
  } else {
    term.scrollLines(lines);
  }
}

function scrollPixels(px) {
  const cell = cellHeight();
  if (!cell) return;
  residue += px;
  const lines = Math.trunc(residue / cell);
  if (!lines) return;
  residue -= lines * cell;
  scrollLines(lines);
}

function stopGlide() {
  cancelAnimationFrame(glideFrame);
  glideFrame = 0;
  glideVelocity = 0;
}

function glide() {
  glideFrame = requestAnimationFrame(() => {
    glideVelocity *= FRICTION;
    if (Math.abs(glideVelocity) < GLIDE_FLOOR) return stopGlide();
    scrollPixels(-glideVelocity * 16);
    glide();
  });
}

// Pinch to resize the text, the same gesture as zooming the page — except the
// grid reflows to the new size instead of the layout being scaled.
let pinch = null;
const spread = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);

el.term.addEventListener('touchstart', (e) => {
  stopGlide();
  residue = 0;
  if (e.touches.length === 2) {
    drag = null;
    pinch = { from: spread(e.touches), size: term.options.fontSize };
  } else if (e.touches.length === 1) {
    pinch = null;
    pointer = { x: e.touches[0].clientX, y: e.touches[0].clientY };
    drag = { y: e.touches[0].clientY, at: e.timeStamp, velocity: 0, distance: 0 };
  }
}, { passive: true });

el.term.addEventListener('touchmove', (e) => {
  if (pinch && e.touches.length === 2) {
    e.preventDefault();
    setFont(pinch.size * (spread(e.touches) / pinch.from));
    return;
  }
  if (!drag || e.touches.length !== 1) return;
  const y = e.touches[0].clientY;
  pointer = { x: e.touches[0].clientX, y };
  const dy = y - drag.y;
  const dt = Math.max(1, e.timeStamp - drag.at);
  drag.y = y;
  drag.at = e.timeStamp;
  // Smoothed, so one jittery last sample doesn't decide the flick.
  drag.velocity = 0.7 * (dy / dt) + 0.3 * drag.velocity;
  drag.distance += Math.abs(dy);
  // Dragging the content down reveals what is above it.
  scrollPixels(-dy);
  e.preventDefault();
}, { passive: false });

el.term.addEventListener('touchend', (e) => {
  // Glide is fine when the gesture is really scrolling something — xterm's own
  // scrollback, or an app that took the mouse. It is not fine when the gesture
  // has decayed into keypresses, where a flick would keep walking a menu
  // after your finger has left.
  const canGlide = term.modes.mouseTrackingMode !== 'none' ||
                   term.buffer.active.type !== 'alternate';
  // A finger that stopped before it lifted was not flicking, whatever its
  // speed on the last move.
  const flicked = drag && e.timeStamp - drag.at < FLICK_PAUSE;
  if (canGlide && flicked && drag.distance > 8 && Math.abs(drag.velocity) > FLICK_MIN) {
    glideVelocity = drag.velocity;
    glide();
  }
  // Remembered for the tap-to-focus handler, which must not raise the keyboard
  // at the end of a scroll.
  lastGestureWasScroll = !!drag && drag.distance > 8;
  drag = null;
  pinch = null;
}, { passive: true });

// ── project tabs ────────────────────────────────────────────────────────────

// A tab is a whole session — its own PTY, its own history, its own project —
// and the server owns the list. Two things follow from that.
//
// First, nothing here decides what a tab is called. The server reads the name
// off the terminal (tmux session, launcher, whatever the command is), so
// picking a project renames its tab and quitting back to the launcher renames
// it again, without this file knowing any of those words.
//
// Second, which tab is in front is the server's state too, not the browser's.
// A phone and a laptop pointed at this are two views of one thing, so a switch
// on either moves both: every browser follows `active` when it sees it change.

let tabs = [];
let activeTab = null;
let attached = null;    // the tab this socket is actually bound to
let maxTabs = Infinity;
let claimNext = false;  // a tab we asked for, whose id we do not know yet

// ?attach=<project>[&agent=…] — Radio Room's agent cards link here. Asked
// for once, after the first attach, and dropped from the address bar so a
// reload is just a reload.
let pendingOpen = (() => {
  const q = new URLSearchParams(location.search);
  const project = q.get('attach');
  return project ? { project, agent: q.get('agent') || undefined } : null;
})();
if (pendingOpen) history.replaceState(null, '', location.pathname + location.hash);
let laidOut = '';       // the tab order currently in the DOM
const tabNodes = new Map();

function mark(node, name, on) {
  if (on) node.dataset[name] = '1'; else delete node.dataset[name];
}

function tabNode(id) {
  let node = tabNodes.get(id);
  if (node) return node;
  node = document.createElement('button');
  node.tabIndex = -1;
  node.dataset.id = id;
  const label = document.createElement('span');
  label.className = 'label';
  const close = document.createElement('span');
  close.className = 'close';
  close.textContent = '\u00d7';
  node.append(label, close);
  tabNodes.set(id, node);
  return node;
}

function renderTabs() {
  // Names are re-read on a poll, so most updates change nothing but text.
  // Only rebuild the strip when the tabs themselves changed, or a rename would
  // throw away where you had scrolled to.
  const order = tabs.map((tab) => tab.id).join(' ');
  if (order !== laidOut) {
    laidOut = order;
    el.tablist.replaceChildren(...tabs.map((tab) => tabNode(tab.id)));
    for (const id of [...tabNodes.keys()]) {
      if (!tabs.some((tab) => tab.id === id)) tabNodes.delete(id);
    }
  }
  for (const tab of tabs) {
    const node = tabNodes.get(tab.id);
    node.firstChild.textContent = tab.name;
    node.title = tab.name;
    mark(node, 'active', tab.id === activeTab);
    mark(node, 'dead', !tab.alive);
  }
  mark(el.tabs, 'single', tabs.length < 2);
  el.newtab.hidden = tabs.length >= maxTabs;

  const current = tabNodes.get(activeTab);
  if (current) current.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  // The tab name is a better answer than any title the shell would set: it is
  // the project, which is what a row of browser tabs needs to tell apart.
  document.title = (current && current.firstChild.textContent) || 'launcher';
}

function takeTabs(msg) {
  tabs = msg.tabs || [];
  activeTab = msg.active || null;
  if (typeof msg.max === 'number') maxTabs = msg.max;
  // The tab we just asked the server to make, now that it has an id.
  if (claimNext && activeTab) { attached = activeTab; claimNext = false; }
  renderTabs();
  // Follow the front tab, whether it moved because of a tap here or on
  // another device — or because the tab we were on was closed out from under
  // us and the server picked its neighbour.
  if (activeTab && activeTab !== attached) selectTab(activeTab);
}

function selectTab(id) {
  // Claimed optimistically: the server confirms with an `attach`, and without
  // this the tabs broadcast that precedes it would ask for the switch twice.
  attached = id;
  send({ t: 'select', tab: id, cols: term.cols, rows: term.rows });
}

// Unlike the key row, which refuses the default on everything, the strip only
// refuses it on the mouse events. Those are synthesised after a touch has been
// judged not to be a scroll, so preventing them keeps focus — and the soft
// keyboard — exactly where it was, while leaving the gesture free to pan the
// strip instead.
for (const type of ['mousedown', 'click']) {
  el.tabs.addEventListener(type, (ev) => {
    if (ev.target.closest('button')) ev.preventDefault();
  }, { passive: false });
}

// On click rather than pointerdown, so that flicking the strip sideways past a
// tab does not select it.
el.tabs.addEventListener('click', (ev) => {
  if (ev.target.closest('#newtab')) {
    claimNext = true;
    send({ t: 'newtab', cols: term.cols, rows: term.rows });
    return;
  }
  const node = ev.target.closest('[data-id]');
  if (!node) return;
  if (ev.target.closest('.close')) send({ t: 'closetab', tab: node.dataset.id });
  else if (node.dataset.id !== activeTab) selectTab(node.dataset.id);
});

// ── connection ──────────────────────────────────────────────────────────────

function showVeil(text, action) {
  el.veilText.textContent = text;
  if (action) el.veil.dataset.action = '1'; else delete el.veil.dataset.action;
  el.veil.onclick = action || null;
  el.veil.hidden = false;
}
function hideVeil() {
  el.veil.hidden = true;
  el.veil.onclick = null;
  delete el.veil.dataset.action;
}

function connect() {
  clearTimeout(reconnectTimer);
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${scheme}://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    retry = 0;
    exited = false;
    lastSize = { cols: 0, rows: 0 };
    send({ t: 'hello', cols: term.cols, rows: term.rows });
  };

  ws.onmessage = (ev) => {
    if (typeof ev.data !== 'string') {
      // Uint8Array rather than a string: xterm's decoder carries UTF-8 state
      // across writes, so a multi-byte character split across two frames still
      // arrives as one character.
      term.write(new Uint8Array(ev.data));
      return;
    }
    const msg = JSON.parse(ev.data);
    if (msg.t === 'tabs') {
      takeTabs(msg);
    } else if (msg.t === 'attach') {
      // Replayed history is the whole session from the server's buffer, so the
      // terminal has to start empty or it would be appended to what is there.
      attached = msg.tab || null;
      // Whatever we are attached to now is running: a tab whose shell had
      // exited is restarted on the way in. Clearing this is what lets a
      // reconnect happen again after one has ended.
      exited = false;
      term.reset();
      hideVeil();
    } else if (msg.t === 'synced') {
      term.scrollToBottom();
      if (pendingOpen) {
        const want = pendingOpen;
        pendingOpen = null;
        claimNext = true;
        send({ t: 'open', ...want, cols: term.cols, rows: term.rows });
      }
      // Only claim focus if nothing else holds it. Grabbing it on every
      // reconnect would raise the soft keyboard on someone who had put it away.
      if (!document.activeElement || document.activeElement === document.body) {
        term.focus();
      }
    } else if (msg.t === 'notice') {
      flash(msg.text);
    } else if (msg.t === 'mic') {
      if (!msg.ok) {
        stopTalking();
        flash('no microphone on the server — ' + (msg.error || 'unknown error'));
      }
    } else if (msg.t === 'exit') {
      exited = true;
      showVeil('session ended — tap to start a new one', () => {
        hideVeil();
        send({ t: 'restart', cols: term.cols, rows: term.rows });
      });
    }
  };

  ws.onclose = () => {
    // The server let go of the mic when the socket went; follow it.
    stopTalking();
    if (exited) return;   // the veil already says something more useful
    retry += 1;
    const delay = Math.min(8000, 250 * Math.pow(1.7, retry - 1));
    // A dropped frame of connectivity is not worth a notice; say something
    // only once it has lasted long enough to be worth knowing about.
    if (retry > 2) showVeil('reconnecting…');
    reconnectTimer = setTimeout(connect, delay);
  };

  ws.onerror = () => { /* onclose does the recovery */ };
}

// A phone that has been asleep wakes with a socket the OS quietly killed;
// reconnect on the way back rather than waiting for a timeout to notice.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && !exited) { retry = 0; connect(); }
});
window.addEventListener('online', () => { retry = 0; connect(); });
window.addEventListener('focus', () => { if (!exited) connect(); });

// ── input ───────────────────────────────────────────────────────────────────

term.onData((data) => send({ t: 'i', d: data }));
// onBinary hands over a "binary string" — one byte per char, values up to
// 0xff. Encoding that as UTF-8 would rewrite every byte above 0x7f, so it
// travels base64 and the server writes the bytes through untouched.
term.onBinary((data) => send({ t: 'ib', d: btoa(data) }));

// Sticky ctrl: tap it, then the next key. Holding a modifier while reaching for
// a letter is not a thing you can do one-handed on a phone.
let ctrlArmed = false;
const ctrlButton = el.keys.querySelector('[data-ctrl]');

function setCtrl(on) {
  ctrlArmed = on;
  if (on) ctrlButton.dataset.armed = '1'; else delete ctrlButton.dataset.armed;
}

function sendCtrl(ch) {
  const code = ch.toUpperCase().charCodeAt(0);
  if (code >= 64 && code <= 95) send({ t: 'i', d: String.fromCharCode(code - 64) });
  else if (ch === ' ') send({ t: 'i', d: '\x00' });
  else if (ch === '?') send({ t: 'i', d: '\x7f' });
  else send({ t: 'i', d: ch });
}

// Leaving a session, for a keyboard. The touch equivalent is the ⏏ key.
// Ctrl+Shift+L is not something a terminal application expects to receive, so
// taking it costs nothing.
function backToLauncher() {
  showVeil('returning to the launcher…');
  send({ t: 'restart' });
}

term.attachCustomKeyEventHandler((ev) => {
  if (ev.type === 'keydown' && ev.ctrlKey && ev.shiftKey &&
      (ev.key === 'L' || ev.key === 'l')) {
    ev.preventDefault();
    backToLauncher();
    return false;
  }
  if (!ctrlArmed || ev.type !== 'keydown') return true;
  if (ev.key.length !== 1) return true;   // a real modifier or a named key
  setCtrl(false);
  sendCtrl(ev.key);
  ev.preventDefault();
  return false;
});

// Arrow keys change encoding when an application turns on DECCKM, which the
// launcher, tmux and readline all do — so ask the terminal which mode it is in
// rather than hard-coding one of the two.
function arrow(letter, ctrl, shift) {
  // ctrl+arrow is word-wise motion in claude's prompt and in readline, and it
  // only has the one encoding. shift+arrow is the same idea for the shift
  // modifier; ctrl+shift stacks the two into the one combined form.
  if (ctrl && shift) return '\x1b[1;6' + letter;
  if (ctrl) return '\x1b[1;5' + letter;
  if (shift) return '\x1b[1;2' + letter;
  return (term.modes.applicationCursorKeysMode ? '\x1bO' : '\x1b[') + letter;
}

if (isTouch) el.keys.hidden = false;

// Focus must never leave the terminal when a key is tapped. A <button> takes
// focus on tap, which blurs xterm's textarea and drops the soft keyboard — and
// putting it back afterwards just means the keyboard animates away and returns
// on every single arrow press. Which of these events carries the focus change
// differs by browser, so refuse the default on all of them.
for (const type of ['pointerdown', 'mousedown', 'touchstart', 'click']) {
  el.keys.addEventListener(type, (ev) => {
    if (ev.target.closest('button')) ev.preventDefault();
  }, { passive: false });
}

el.keys.addEventListener('pointerdown', (ev) => {
  const button = ev.target.closest('button');
  if (!button) return;

  if (button.hasAttribute('data-ctrl')) { setCtrl(!ctrlArmed); return; }
  if (button.hasAttribute('data-blur')) { setCtrl(false); term.blur(); return; }
  if (button.hasAttribute('data-launcher')) { setCtrl(false); backToLauncher(); return; }
  if (button.hasAttribute('data-paste')) { setCtrl(false); openPaste(); return; }

  const literal = button.getAttribute('data-send');
  const direction = button.getAttribute('data-arrow');
  const shift = button.hasAttribute('data-shift');
  // Only the arrows combine with ctrl; esc, tab and enter have no useful ctrl
  // form, so tapping one just spends the armed modifier and does the plain
  // thing.
  const keys = literal !== null ? literal : arrow(direction, ctrlArmed, shift);
  setCtrl(false);
  send({ t: 'i', d: keys });
  // xterm empties its textarea when a real return passes through it, because
  // that line is now the shell's problem and not the keyboard's. The soft ⏎
  // goes straight over the socket and never touches the textarea, so do it
  // here — otherwise the keyboard keeps suggesting against, and correcting
  // into, a line that has already run.
  if (keys.includes('\r')) term.textarea.value = '';
  // Deliberately no term.focus() here. Input goes over the socket, not through
  // the textarea, so the keys work whatever has focus — and leaving focus
  // alone means the keyboard stays exactly as you left it, up or down.
});

// Tapping the terminal should raise the keyboard, including when the tap lands
// on a spot xterm treats as a drag that ended where it started.
el.term.addEventListener('pointerup', () => {
  if (term.hasSelection() || lastGestureWasScroll) return;
  lastGestureWasScroll = false;
  term.focus();
});

// ── copy ─────────────────────────────────────────────────────────────────────

// With tmux's `mouse on` a drag selects in tmux's copy-mode, not here, so
// xterm never has a selection of its own to copy. tmux sends the copied text
// back as OSC 52 (`set-clipboard on` in ~/.tmux.conf) and this writes it to
// the clipboard of whatever device is viewing the page. The write follows
// the release of the drag closely enough to count as part of that gesture.
// A query (`?`) is never answered: that would let anything printed to the
// terminal read the viewer's clipboard.
term.parser.registerOscHandler(52, (data) => {
  const payload = data.slice(data.indexOf(';') + 1);
  if (payload === '?' || !navigator.clipboard) return true;
  try {
    const bytes = Uint8Array.from(atob(payload), (c) => c.charCodeAt(0));
    navigator.clipboard.writeText(new TextDecoder().decode(bytes)).catch(() => {});
  } catch { /* not base64; ignore it */ }
  return true;
});

// ── paste ────────────────────────────────────────────────────────────────────

// iOS Safari will not surface a long-press "Paste" callout on the terminal
// itself: #term carries `touch-action: none` for the scroll/glide gesture,
// and `navigator.clipboard` is unavailable because the page is served over
// plain HTTP on the tailnet (not a secure context). So the paste key raises
// this sheet with a real, focusable textarea that the OS callout will target,
// and a paste into it is handed to `term.paste` — which honours bracketed
// paste mode the way a keyboard paste would, rather than dumping raw bytes.

function closePaste() {
  el.paste.hidden = true;
  el.pasteField.value = '';
  term.focus();
}

function openPaste() {
  el.paste.hidden = false;
  // Focus from within the pointer gesture so iOS will raise the keyboard and
  // accept the long-press that follows. `select()` gives the callout an
  // empty selection to anchor to, so it does not fire on existing text.
  el.pasteField.focus();
  el.pasteField.select();
}

el.pasteField.addEventListener('paste', (ev) => {
  ev.preventDefault();
  const image = imageIn(ev.clipboardData);
  const text = ev.clipboardData ? ev.clipboardData.getData('text/plain') : '';
  if (image) sendImage(image);
  else if (text) term.paste(text);
  closePaste();
});

// ── images ───────────────────────────────────────────────────────────────────

// The agent cannot see this device's clipboard or photos, so an image is
// uploaded (/api/image), saved on the server where the agent can read it, and
// its path pasted into the terminal — which Claude Code turns into an attached
// image, as Ctrl+V does at the desk. It comes from the sheet's image button
// (photos, camera, files), a long-press paste into the sheet, a browser paste
// on the terminal, or a drop. An agent inside a container can't see the
// server's files; the voice page's terminal copies the image in for those.

// Claude reads PNG, JPEG, GIF and WebP. Anything else the browser can show —
// an iPhone's HEIC — is redrawn as a JPEG first.
const CLAUDE_IMAGES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];

function imageIn(data) {
  if (!data) return null;
  for (const item of data.items || []) {
    if (item.kind === 'file' && item.type.startsWith('image/')) return item.getAsFile();
  }
  return null;
}

async function claudeImage(blob) {
  if (CLAUDE_IMAGES.includes(blob.type)) return blob;
  const bitmap = await createImageBitmap(blob);
  const canvas = document.createElement('canvas');
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  canvas.getContext('2d').drawImage(bitmap, 0, 0);
  return new Promise((ok, fail) => canvas.toBlob(
    (b) => (b ? ok(b) : fail(new Error("couldn't convert the image"))), 'image/jpeg', 0.9));
}

async function sendImage(blob) {
  showVeil('sending image…');
  try {
    const body = await claudeImage(blob);
    const r = await fetch('api/image', { method: 'POST', headers: { 'Content-Type': body.type }, body });
    const reply = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
    if (!r.ok) throw new Error(reply.error || `HTTP ${r.status}`);
    hideVeil();
    // term.paste, not a plain send: bracketed paste is how Claude Code tells
    // a pasted path from typing.
    term.paste(reply.path);
  } catch (err) {
    flash(`image not sent: ${err.message}`);
  }
}

function pickImage() {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*';
  input.onchange = () => { if (input.files[0]) sendImage(input.files[0]); };
  input.click();
}

// A click, not a pointerdown: on a touch screen only the lift counts as a
// gesture, and a file picker opens for nothing less.
el.pasteImage.addEventListener('click', (ev) => {
  ev.preventDefault();
  // Put away without closePaste's focus: the keyboard would rise under the picker.
  el.paste.hidden = true;
  el.pasteField.value = '';
  pickImage();
});

// Capture, so xterm's own paste handler never sees the image (it would paste
// nothing, or a file name).
el.term.addEventListener('paste', (ev) => {
  const image = imageIn(ev.clipboardData);
  if (!image) return;
  ev.preventDefault();
  ev.stopPropagation();
  sendImage(image);
}, true);

el.term.addEventListener('dragover', (ev) => ev.preventDefault());
el.term.addEventListener('drop', (ev) => {
  const image = imageIn(ev.dataTransfer);
  if (!image) return;
  ev.preventDefault();
  sendImage(image);
});

el.pasteCancel.addEventListener('click', (ev) => {
  ev.preventDefault();
  closePaste();
});

// A tap on the scrim outside the card dismisses the sheet.
el.paste.addEventListener('click', (ev) => {
  if (ev.target === el.paste) closePaste();
});

// Escape works on a keyboard; the cancel button covers the touch case.
el.pasteField.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') { ev.preventDefault(); closePaste(); }
});

// ── voice ───────────────────────────────────────────────────────────────────

// Claude Code's voice mode records from the server's default microphone, and
// that is a long way from the phone in your hand. So holding 🎤 does two
// things at once: it streams this browser's microphone to the server, which
// makes it the default source for as long as you hold on (mic.py), and it
// sends space at key-repeat rate, which is exactly what Claude's push-to-talk
// sees from a held space bar. Letting go stops the repeat; Claude notices the
// key is up and transcribes.
//
// The order matters. The `mic` message goes first and the server swaps the
// default before reading the next one, so the space that makes Claude open the
// device always finds the browser behind it.
//
// This needs HTTPS — browsers will not hand the microphone to a plain-HTTP
// page — which is why the server serves its tailnet certificate.

const SPACE_REPEAT = 50;      // ms; a fast key-repeat, well inside Claude's window
const TAIL = 400;             // ms of audio after release: the end of the last word
const MIC_IDLE = 30000;       // ms before the open microphone is put away

const mic = { ctx: null, stream: null, node: null, opening: null };
let talking = false;          // the button is held and audio is going up
let held = false;             // the button is down, whether or not audio is ready
let spaceTimer = 0;
let tailTimer = 0;
let idleTimer = 0;

function flash(text) {
  showVeil(text);
  setTimeout(() => { if (el.veilText.textContent === text) hideVeil(); }, 3000);
}

// Opened once and kept warm between presses, so the first word of the next
// one is not spent waiting for the microphone to start.
function openMic() {
  if (mic.node) return Promise.resolve();
  if (mic.opening) return mic.opening;
  // Created here, inside the press, because iOS only lets an AudioContext run
  // if it was made or resumed by a gesture — and after an await it is not.
  if (!mic.ctx) mic.ctx = new AudioContext();
  mic.ctx.resume();
  mic.opening = (async () => {
    mic.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    await mic.ctx.audioWorklet.addModule('mic-worklet.js');
    const node = new AudioWorkletNode(mic.ctx, 'pcm16');
    node.port.onmessage = (ev) => {
      if (!talking || !ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(ev.data.pcm);
      el.mic.style.setProperty('--level', Math.min(1, ev.data.peak * 2).toFixed(2));
    };
    mic.ctx.createMediaStreamSource(mic.stream).connect(node);
    // Silent, but connected: a node nothing pulls on may never be run.
    node.connect(mic.ctx.destination);
    mic.node = node;
  })();
  return mic.opening.finally(() => { mic.opening = null; });
}

function closeMic() {
  if (mic.stream) mic.stream.getTracks().forEach((track) => track.stop());
  if (mic.ctx) mic.ctx.close();
  mic.ctx = mic.stream = mic.node = null;
}

async function startTalking() {
  held = true;
  clearTimeout(idleTimer);
  if (tailTimer) {
    // Pressed again before the last one finished: carry on from where it is.
    clearTimeout(tailTimer);
    tailTimer = 0;
  }
  if (!window.isSecureContext || !navigator.mediaDevices) {
    held = false;
    flash('voice needs the https:// address');
    return;
  }
  el.mic.dataset.busy = '1';
  try {
    await openMic();
  } catch (e) {
    held = false;
    delete el.mic.dataset.busy;
    closeMic();
    flash('microphone refused — ' + (e.message || e.name));
    return;
  }
  delete el.mic.dataset.busy;
  // A quick tap can be over before the microphone was.
  if (!held || !ws || ws.readyState !== WebSocket.OPEN) {
    if (!talking) idleTimer = setTimeout(closeMic, MIC_IDLE);
    return;
  }

  if (!talking) send({ t: 'mic', on: true, rate: mic.ctx.sampleRate });
  talking = true;
  el.mic.dataset.live = '1';
  send({ t: 'i', d: ' ' });
  clearInterval(spaceTimer);
  spaceTimer = setInterval(() => send({ t: 'i', d: ' ' }), SPACE_REPEAT);
}

function stopTalking() {
  held = false;
  clearInterval(spaceTimer);
  spaceTimer = 0;
  if (!talking) return;
  clearTimeout(tailTimer);
  tailTimer = setTimeout(() => {
    tailTimer = 0;
    talking = false;
    delete el.mic.dataset.live;
    el.mic.style.setProperty('--level', 0);
    send({ t: 'mic', on: false });
    clearTimeout(idleTimer);
    idleTimer = setTimeout(closeMic, MIC_IDLE);
  }, TAIL);
}

el.mic.addEventListener('pointerdown', (ev) => {
  ev.preventDefault();
  // Captured, so sliding a thumb off the button while talking does not end
  // the sentence; only lifting it does.
  el.mic.setPointerCapture(ev.pointerId);
  startTalking();
});
for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
  el.mic.addEventListener(type, () => { if (held || talking) stopTalking(); });
}
el.mic.addEventListener('contextmenu', (ev) => ev.preventDefault());
// A phone put away mid-press never sends the pointerup.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') stopTalking();
});

connect();

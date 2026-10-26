"""A microphone for this machine that is really a browser on the tailnet.

Claude Code's voice mode records from whatever the default capture device is
— its native module, arecord and sox all end up at PipeWire's default source —
and nothing about that can be pointed somewhere else per process. So the
browser's audio has to *become* a capture device:

    browser ── ws binary frames ── pacat ──▶ null sink ──▶ monitor
                s16le mono                                   │
                                          remap-source ◀─────┘
                                          "ssh_client mic"  ◀── default source,
                                                                 while held

The remap is there so the thing at the end is a real source rather than a
monitor, which some capture paths skip when they go looking for a microphone.

It is the default source only while someone is talking. A held mic button
swaps it in, and letting go puts back whatever was there — after a pause,
because changing the default moves streams that follow it, and Claude is
still recording the tail of the sentence when the button comes up.

The modules are loaded on first use and unloaded on exit. A server that died
without unloading them leaves them behind; the next one finds them by name and
takes them over rather than stacking a second pair.
"""

import subprocess
import threading

SINK = "ssh_client_voice"
SOURCE = "ssh_client_mic"

# Long enough that Claude has noticed the key is up and stopped recording
# before its stream is moved back to the real microphone.
RESTORE_AFTER = 4.0


class MicError(Exception):
    pass


def pactl(*args: str) -> str:
    try:
        done = subprocess.run(
            ["pactl", *args], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MicError(f"pactl {args[0]}: {exc}") from exc
    if done.returncode != 0:
        raise MicError(f"pactl {args[0]}: {done.stderr.strip() or done.returncode}")
    return done.stdout.strip()


class VirtualMic:
    """The one virtual microphone, shared by every browser.

    Holders are counted rather than owned: two devices holding the button at
    once both feed the same pipe, and the default comes back only when the
    last of them lets go.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.holders: set = set()
        self.modules: list[str] = []
        self.player: subprocess.Popen | None = None
        self.rate = 0
        self.previous: str | None = None  # the default source we displaced
        self.restore_timer: threading.Timer | None = None

    # -- the PipeWire side --

    def ensure_devices(self) -> None:
        listed = pactl("list", "short", "modules").splitlines()
        ours = [line.split("\t")[0] for line in listed
                if f"sink_name={SINK}" in line or f"source_name={SOURCE}" in line]
        if len(ours) == 2:
            self.modules = ours
            return
        for module in ours:  # half a pair is no use; start clean
            pactl("unload-module", module)
        self.modules = [
            pactl("load-module", "module-null-sink", f"sink_name={SINK}",
                  "channels=1",
                  "sink_properties=device.description=ssh_client-voice"),
        ]
        self.modules.append(
            pactl("load-module", "module-remap-source", f"master={SINK}.monitor",
                  f"source_name={SOURCE}", "channels=1",
                  "source_properties=device.description=ssh_client-mic"),
        )

    def start_player(self, rate: int) -> None:
        if self.player and self.player.poll() is None and rate == self.rate:
            return
        self.stop_player()
        try:
            self.player = subprocess.Popen(
                ["pacat", "--playback", f"--device={SINK}", "--format=s16le",
                 f"--rate={rate}", "--channels=1", "--latency-msec=40",
                 "--client-name=ssh_client", "--stream-name=browser mic"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise MicError(f"pacat: {exc}") from exc
        self.rate = rate

    def stop_player(self) -> None:
        if self.player is None:
            return
        try:
            self.player.stdin.close()
        except OSError:
            pass
        try:
            self.player.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.player.kill()
        self.player = None

    # -- holding and letting go --

    def hold(self, who, rate: int) -> None:
        """Make the browser's audio the default microphone. Raises MicError."""
        with self.lock:
            if self.restore_timer:
                self.restore_timer.cancel()
                self.restore_timer = None
            self.ensure_devices()
            self.start_player(rate)
            current = pactl("get-default-source")
            if current != SOURCE:
                self.previous = current
                pactl("set-default-source", SOURCE)
            self.holders.add(who)

    def feed(self, who, pcm: bytes) -> None:
        if who not in self.holders or self.player is None:
            return
        try:
            self.player.stdin.write(pcm)
            self.player.stdin.flush()
        except (OSError, ValueError):
            pass  # pacat died; the next hold starts another

    def release(self, who) -> None:
        with self.lock:
            if who not in self.holders:
                return
            self.holders.discard(who)
            if self.holders:
                return
            self.restore_timer = threading.Timer(RESTORE_AFTER, self.restore)
            self.restore_timer.daemon = True
            self.restore_timer.start()

    def restore(self) -> None:
        with self.lock:
            self.restore_timer = None
            if self.holders:
                return
            self.stop_player()
            if self.previous:
                try:
                    if pactl("get-default-source") == SOURCE:
                        pactl("set-default-source", self.previous)
                except MicError as exc:
                    print(f"mic: could not restore {self.previous}: {exc}", flush=True)
                self.previous = None

    def close(self) -> None:
        """Put everything back as it was. For shutdown."""
        with self.lock:
            self.holders.clear()
            if self.restore_timer:
                self.restore_timer.cancel()
                self.restore_timer = None
        self.restore()
        for module in reversed(self.modules):
            try:
                pactl("unload-module", module)
            except MicError:
                pass
        self.modules = []

    def status(self) -> dict:
        return {"held": len(self.holders), "rate": self.rate,
                "devices": bool(self.modules)}

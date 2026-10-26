"""Minimal server-side RFC 6455 WebSocket, stdlib only.

Just enough of the protocol to carry a terminal: text and binary messages,
fragmentation, ping/pong and a clean close. No extensions, no permessage-deflate
— terminal traffic is small and latency matters more than bytes.

The rest of Radio Room (hub.py, proj, the voice server) is dependency-free stdlib
Python, so this file keeps that property rather than pulling in `websockets`.
"""

import base64
import hashlib
import socket
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA

# A terminal never sends us a large message; anything bigger is a bug or an
# attack, and we would rather drop the connection than buffer it.
MAX_MESSAGE = 1 << 20


class WebSocketError(Exception):
    pass


def accept_key(client_key: str) -> str:
    """The Sec-WebSocket-Accept value for a client's Sec-WebSocket-Key."""
    digest = hashlib.sha1((client_key.strip() + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def is_upgrade(headers) -> bool:
    return (
        headers.get("Upgrade", "").lower() == "websocket"
        and "upgrade" in headers.get("Connection", "").lower()
        and bool(headers.get("Sec-WebSocket-Key"))
    )


class WebSocket:
    """One accepted connection.

    `recv()` is called from the owning thread only. `send()` is safe from any
    thread — the PTY pump and the ping timer both use it — because every frame
    goes out under one lock, so frames are never interleaved on the wire.
    """

    def __init__(self, sock: socket.socket, reader=None):
        self.sock = sock
        # Reads go through `reader` rather than the socket. The HTTP handler
        # that accepted the upgrade owns a buffered file over this same socket,
        # and a browser puts its first frames in the same TCP segment as the
        # handshake — so those bytes are already sitting in that buffer, and
        # reading the socket directly would start the frame parser one message
        # in and desynchronise it immediately.
        self.reader = reader if reader is not None else sock.makefile("rb")
        self._send_lock = threading.Lock()
        self._closed = False

    # ── receiving ────────────────────────────────────────────────────────────

    def _read_exact(self, n: int) -> bytes:
        chunks = []
        remaining = n
        while remaining:
            chunk = self.reader.read(remaining)
            if not chunk:
                raise WebSocketError("peer closed mid-frame")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_frame(self):
        b0, b1 = self._read_exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F

        if length == 126:
            (length,) = struct.unpack("!H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._read_exact(8))
        if length > MAX_MESSAGE:
            raise WebSocketError(f"frame of {length} bytes exceeds cap")

        # Clients MUST mask; an unmasked client frame is a protocol violation.
        if not masked:
            raise WebSocketError("client frame was not masked")
        mask = self._read_exact(4)
        payload = bytearray(self._read_exact(length))
        for i in range(length):
            payload[i] ^= mask[i & 3]
        return fin, opcode, bytes(payload)

    def recv(self):
        """Next application message as (opcode, payload), or None once closed.

        Control frames are answered here and never surface to the caller.
        """
        frames = []
        message_op = None
        while True:
            fin, opcode, payload = self._read_frame()

            if opcode == OP_CLOSE:
                self.close()
                return None
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue

            if opcode == OP_CONT:
                if message_op is None:
                    raise WebSocketError("continuation with nothing to continue")
            else:
                if message_op is not None:
                    raise WebSocketError("new message interleaved with a fragment")
                message_op = opcode

            frames.append(payload)
            if sum(map(len, frames)) > MAX_MESSAGE:
                raise WebSocketError("fragmented message exceeds cap")
            if fin:
                return message_op, b"".join(frames)

    # ── sending ──────────────────────────────────────────────────────────────

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x80 | opcode, length)
        elif length < (1 << 16):
            header = struct.pack("!BBH", 0x80 | opcode, 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 127, length)
        with self._send_lock:
            if self._closed:
                return
            try:
                self.sock.sendall(header + payload)
            except OSError as e:
                self._closed = True
                raise WebSocketError(f"send failed: {e}") from e

    def send_bytes(self, data: bytes) -> None:
        self._send_frame(OP_BINARY, data)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def ping(self) -> None:
        self._send_frame(OP_PING, b"")

    def close(self, code: int = 1000) -> None:
        if self._closed:
            return
        try:
            self._send_frame(OP_CLOSE, struct.pack("!H", code))
        except (WebSocketError, OSError):
            pass
        with self._send_lock:
            self._closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    @property
    def closed(self) -> bool:
        return self._closed

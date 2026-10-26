"""Where a server listens.

Every server here is unauthenticated or holds one shared secret, so the
address it binds is the boundary. A `bind` setting is one of:

    "local"       127.0.0.1 only: this machine and nothing else (the default)
    "tailscale"   127.0.0.1 and the tailscale0 address: this machine and the
                  tailnet, not the LAN
    "any"         0.0.0.0: every interface, the LAN included
    an address    that address only, e.g. "10.0.0.5" or "0.0.0.0"

Stdlib only.
"""

from __future__ import annotations

import fcntl
import ipaddress
import socket
import struct

TAILSCALE_IF = "tailscale0"
LOOPBACK = "127.0.0.1"


class BindError(Exception):
    pass


def interface_ip(name: str) -> str | None:
    """IPv4 address of an interface, or None if it has none."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(
            sock.fileno(),
            0x8915,  # SIOCGIFADDR
            struct.pack("256s", name.encode()[:15]),
        )
        return socket.inet_ntoa(packed[20:24])
    except OSError:
        return None
    finally:
        sock.close()


def addresses(bind: str | None, tailscale_ip=lambda: interface_ip(TAILSCALE_IF)) -> list[str]:
    """The addresses a `bind` setting means. BindError if it means the
    tailnet and there isn't one — never a quiet fallback to somewhere else."""
    b = (bind or "local").strip()
    if b in ("local", "localhost"):
        return [LOOPBACK]
    if b == "any":
        return ["0.0.0.0"]
    if b == "tailscale":
        ts = tailscale_ip()
        if ts is None:
            raise BindError(f'bind = "tailscale", but {TAILSCALE_IF} has no address '
                            f"— is Tailscale up?")
        return [LOOPBACK, ts]
    return [b]


def is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def exposed(addrs: list[str], tailscale_ip=lambda: interface_ip(TAILSCALE_IF)) -> list[str]:
    """The addresses that reach past this machine and the tailnet — the LAN,
    or everything. Empty means only loopback and Tailscale are listened on."""
    ts = None
    out = []
    for a in addrs:
        if is_loopback(a):
            continue
        if ts is None:
            ts = tailscale_ip() or ""
        if a != ts:
            out.append(a)
    return out

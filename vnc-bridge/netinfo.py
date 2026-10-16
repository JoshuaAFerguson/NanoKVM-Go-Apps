"""Interface addresses via ioctl (stdlib, no `ip` subprocess).

Shared module: edit _shared/netinfo.py, then run `python3 _tools/sync_shared.py`.
"""

from __future__ import annotations

import fcntl
import socket
import struct

SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B


def _ioctl_ipv4(name: str, request: int) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(sock.fileno(), request, struct.pack("256s", name.encode()[:15]))
        return socket.inet_ntoa(packed[20:24])
    except OSError:
        return ""  # interface missing or has no IPv4 address
    finally:
        sock.close()


def interface_ipv4(name: str) -> str:
    return _ioctl_ipv4(name, SIOCGIFADDR)


def interface_netmask(name: str) -> str:
    return _ioctl_ipv4(name, SIOCGIFNETMASK)


def lan_interface() -> str:
    """First of wlan0/eth0 that has an IPv4 address ('' if none)."""
    return next((n for n in ("wlan0", "eth0") if interface_ipv4(n)), "")

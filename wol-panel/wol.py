"""Wake-on-LAN and ping helpers (pure functions + tiny socket wrappers, stdlib only)."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import select
import socket
import struct
import time
from dataclasses import dataclass

WOL_PORTS = (9, 7)
MAX_HOSTS = 30
MAX_NAME = 14
MAC_RE = re.compile(r"^[0-9a-f]{2}([:\-]?)(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}$")  # one separator style
GLOBAL_BROADCAST = "255.255.255.255"


class WolError(ValueError):
    """Host configuration is invalid (shown on screen)."""


@dataclass(frozen=True)
class Host:
    name: str
    mac: str
    ip: str = ""


def normalize_mac(raw: str, name: str) -> str:
    cleaned = raw.strip().lower()
    if not MAC_RE.match(cleaned):
        raise WolError(f"'{name}': invalid MAC address {raw!r}")
    digits = re.sub(r"[:\-]", "", cleaned)
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def _validate_ip(ip: str, name: str) -> str:
    if not ip:
        return ""
    try:
        address = ipaddress.IPv4Address(ip.strip())
    except ipaddress.AddressValueError as exc:
        raise WolError(f"'{name}': invalid IPv4 address {ip!r}") from exc
    if address.is_unspecified or address.is_loopback or address.is_multicast or str(address) == GLOBAL_BROADCAST:
        raise WolError(f"'{name}': {ip} can't be a machine's address")
    return str(address)


def _host(name, mac, ip) -> Host:
    if not isinstance(name, str) or not isinstance(mac, str) or not isinstance(ip or "", str):
        raise WolError(f"host entries need string name/mac/ip: {name!r}")
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    if not name or len(name) > MAX_NAME:
        raise WolError(f"host name must be 1..{MAX_NAME} characters: {name!r}")
    return Host(name, normalize_mac(str(mac or ""), name), _validate_ip(str(ip or ""), name))


def parse_hosts(text: str | None) -> tuple[Host, ...]:
    """`Name=MAC[@IP]` entries separated by `;` or newlines, or a JSON list of {name, mac, ip}."""
    text = (text or "").strip()
    if not text:
        return ()
    if text.startswith("["):
        try:
            items = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WolError(f"WOL_HOSTS JSON is invalid: {exc.msg}") from exc
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise WolError("WOL_HOSTS JSON must be a list of objects")
        hosts = [_host(i.get("name"), i.get("mac"), i.get("ip")) for i in items]
    else:
        hosts = []
        for entry in re.split(r"[;\n]", text):
            entry = entry.strip()
            if not entry:
                continue
            name, sep, rest = entry.partition("=")
            if not sep:
                raise WolError(f"'{name.strip()}': expected Name=MAC[@IP]")
            mac, _, ip = rest.partition("@")
            hosts.append(_host(name, mac, ip))
    if len(hosts) > MAX_HOSTS:
        raise WolError(f"at most {MAX_HOSTS} hosts")
    names = [h.name.lower() for h in hosts]
    if len(names) != len(set(names)):
        raise WolError("host names must be unique (case-insensitive)")
    return tuple(hosts)


def validate_broadcasts(entries: list[str]) -> tuple[list[str], list[str]]:
    """Keep the global broadcast and private-range IPv4 addresses; return (valid, rejected).

    Hostnames are refused (no DNS on tap) and public addresses too, so magic
    packets never leave the local network.
    """
    good, bad = [], []
    for entry in (e.strip() for e in entries):
        if not entry:
            continue
        try:
            address = ipaddress.IPv4Address(entry)
        except ipaddress.AddressValueError:
            bad.append(entry)
            continue
        (good if str(address) == GLOBAL_BROADCAST or address.is_private else bad).append(str(address))
    return good, bad


def magic_packet(mac: str) -> bytes:
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


def directed_broadcast(ip: str, netmask: str) -> str:
    network = ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False)
    return str(network.broadcast_address)


def inet_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def icmp_echo(ident: int, seq: int, payload: bytes = b"nanokvm-wol") -> bytes:
    header = struct.pack("!BBHHH", 8, 0, 0, ident, seq)
    checksum = inet_checksum(header + payload)
    return struct.pack("!BBHHH", 8, 0, checksum, ident, seq) + payload


def is_echo_reply(packet: bytes, ident: int, seq: int, payload: bytes | None = None) -> bool:
    """True if `packet` (IPv4 header + ICMP) is our echo reply; payload, if given, must match."""
    if len(packet) < 20 or packet[0] >> 4 != 4:
        return False
    ihl = (packet[0] & 0x0F) * 4
    if ihl < 20 or len(packet) < ihl + 8:
        return False
    kind, _, _, rid, rseq = struct.unpack("!BBHHH", packet[ihl:ihl + 8])
    if kind != 0 or rid != ident or rseq != seq:
        return False
    return payload is None or packet[ihl + 8:] == payload


def parse_arp(text: str) -> list[tuple[str, str]]:
    """(ip, mac) of complete LAN neighbours from /proc/net/arp (excludes tunnels)."""
    neighbours = []
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 6 and cols[2] != "0x0" and cols[3] != "00:00:00:00:00:00" and cols[5].startswith(("wlan", "eth")):
            neighbours.append((cols[0], cols[3].lower()))
    return neighbours


def status_text(up: bool | None, waking_since: float | None, now: float, has_ip: bool = True) -> str:
    if up:
        return "online"
    if up is None:
        return "checking..." if has_ip else "no IP to ping"
    if waking_since is not None:
        return f"waking {int(now - waking_since)}s"
    return "offline"


# -- sockets (device) -------------------------------------------------------------

def send_wol(mac: str, broadcasts: list[str]) -> int:
    """Send the magic packet to every (pre-validated) broadcast address and port.

    One failing address doesn't stop the rest; raises OSError only if nothing was sent.
    """
    packet = magic_packet(mac)
    sent, errors = 0, []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for address in dict.fromkeys(broadcasts):  # de-duplicate, keep order
            for port in WOL_PORTS:
                try:
                    sock.sendto(packet, (address, port))
                    sent += 1
                except OSError as exc:
                    errors.append(f"{address}:{port} {exc}")
    if not sent:
        raise OSError("; ".join(errors) or "no broadcast addresses")
    return sent


def ping_many(ips: list[str], timeout: float = 1.0) -> dict[str, bool]:
    """One ICMP echo to each IP over a single raw socket; collect replies until timeout.

    Apps run as root, so raw sockets work and no `ping` subprocess is needed.
    """
    ident = os.getpid() & 0xFFFF
    nonce = os.urandom(8)  # payload must echo back, so other processes' replies can't match
    expected = {ip: seq for seq, ip in enumerate(dict.fromkeys(ips), start=1)}
    results = dict.fromkeys(expected, False)
    if not expected:
        return results
    with socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP) as sock:
        for ip, seq in expected.items():
            try:
                sock.sendto(icmp_echo(ident, seq, nonce), (ip, 0))
            except OSError:
                pass  # unreachable network: stays False
        deadline = time.monotonic() + timeout
        while not all(results.values()) and (left := deadline - time.monotonic()) > 0:
            ready, _, _ = select.select([sock], [], [], left)
            if not ready:
                break
            try:
                packet, (src, _) = sock.recvfrom(1024)
            except OSError:
                break  # keep the replies collected so far
            seq = expected.get(src)
            if seq is not None and is_echo_reply(packet, ident, seq, nonce):
                results[src] = True
    return results

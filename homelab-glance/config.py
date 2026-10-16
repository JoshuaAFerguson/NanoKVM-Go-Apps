"""Homelab Glance configuration from the App's env settings (validated)."""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass, field

DEFAULT_REFRESH_S, MIN_REFRESH_S, MAX_REFRESH_S = 30, 10, 3600
MAX_ENTITIES = 12
MAX_QUERIES = 6
PVE_TOKEN_RE = re.compile(r"^[^@\s]+@[^!\s]+![^=\s]+=\S+$")  # user@realm!tokenid=secret
ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
TOKEN_RE = re.compile(r"^[\x21-\x7e]{1,4096}$")  # one printable token: no spaces/newlines
DEFAULT_PROM_QUERIES = (("Targets up", "sum(up)"), ("Targets", "count(up)"))
TITLES = {"ha": "Home Asst", "kuma": "Uptime", "pve": "Proxmox", "prom": "Prometheus",
          "k8s": "Kubernetes", "docker": "Docker"}


class ConfigError(ValueError):
    """Shown on screen; names the setting to fix."""


@dataclass(frozen=True)
class Service:
    kind: str           # ha | kuma | pve | prom | k8s | docker
    url: str
    token: str = ""
    verify_tls: bool = True
    options: dict = field(default_factory=dict)
    ca_file: str = ""

    @property
    def title(self) -> str:
        return TITLES[self.kind]


def _url(env: dict, key: str) -> str:
    raw = (env.get(key) or "").strip()
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"{key} must be an http(s) URL like http://192.168.0.10:8123")
    if parts.username or parts.password:
        raise ConfigError(f"{key} must not contain a username/password; use the token setting")
    if parts.query or parts.fragment:
        raise ConfigError(f"{key} must not contain ?query or #fragment")
    return raw.rstrip("/")


def _token(env: dict, key: str, why: str, required: bool = True) -> str:
    value = (env.get(key) or "").strip()
    if not value and not required:
        return ""
    if not value:
        raise ConfigError(f"{key} is required ({why})")
    if not TOKEN_RE.match(value):
        raise ConfigError(f"{key} must be a single token without spaces or line breaks")
    return value


def _ca(env: dict, prefix: str) -> str:
    path = (env.get(f"{prefix}_CA_FILE") or "").strip()
    if path and not os.path.isfile(path):
        raise ConfigError(f"{prefix}_CA_FILE: no such file {path}")
    return path


def _need(env: dict, key: str, why: str) -> str:
    value = (env.get(key) or "").strip()
    if not value:
        raise ConfigError(f"{key} is required ({why})")
    return value


def _flag(env: dict, key: str) -> bool:
    return (env.get(key) or "").strip().lower() in ("1", "true", "yes", "on")


def _list(text: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in re.split(r"[,;\n]", text or "") if item.strip())


def _queries(text: str) -> tuple[tuple[str, str], ...]:
    pairs = []
    for entry in re.split(r";|\n", text or ""):
        label, sep, query = entry.partition("=")
        if not entry.strip():
            continue
        if not sep or not label.strip() or not query.strip():
            raise ConfigError(f"PROM_QUERIES entries must be Label=PromQL: {entry.strip()!r}")
        pairs.append((label.strip()[:14], query.strip()))
    return tuple(pairs[:MAX_QUERIES]) or DEFAULT_PROM_QUERIES


def load(env: dict) -> list[Service]:
    """Configured services in a fixed order; a service counts as configured when its URL is set."""
    services = []
    if (env.get("HA_URL") or "").strip():
        entities = _list(_need(env, "HA_ENTITIES", "entity IDs to show, e.g. sensor.temp"))
        bad = [e for e in entities if not ENTITY_RE.match(e)]
        if bad:
            raise ConfigError(f"HA_ENTITIES has invalid entity IDs: {', '.join(bad)}")
        services.append(Service("ha", _url(env, "HA_URL"), _token(env, "HA_TOKEN", "long-lived access token"),
                                not _flag(env, "HA_INSECURE"), {"entities": entities[:MAX_ENTITIES]}, _ca(env, "HA")))
    if (env.get("KUMA_URL") or "").strip():
        slug = _need(env, "KUMA_SLUG", "status page slug")
        if not re.match(r"^[A-Za-z0-9_-]+$", slug):
            raise ConfigError("KUMA_SLUG may only contain letters, digits, - and _")
        services.append(Service("kuma", _url(env, "KUMA_URL"), "", not _flag(env, "KUMA_INSECURE"), {"slug": slug},
                                _ca(env, "KUMA")))
    if (env.get("PVE_URL") or "").strip():
        token = _need(env, "PVE_TOKEN", "API token as user@realm!tokenid=secret")
        if not PVE_TOKEN_RE.match(token):
            raise ConfigError("PVE_TOKEN must look like user@realm!tokenid=secret")
        services.append(Service("pve", _url(env, "PVE_URL"), token, not _flag(env, "PVE_INSECURE"), {}, _ca(env, "PVE")))
    if (env.get("PROM_URL") or "").strip():
        services.append(Service("prom", _url(env, "PROM_URL"), _token(env, "PROM_TOKEN", "", required=False),
                                not _flag(env, "PROM_INSECURE"), {"queries": _queries(env.get("PROM_QUERIES", ""))},
                                _ca(env, "PROM")))
    if (env.get("K8S_URL") or "").strip():
        services.append(Service("k8s", _url(env, "K8S_URL"), _token(env, "K8S_TOKEN", "ServiceAccount token"),
                                not _flag(env, "K8S_INSECURE"), {}, _ca(env, "K8S")))
    if (env.get("DOCKER_URL") or "").strip():
        services.append(Service("docker", _url(env, "DOCKER_URL"), "", not _flag(env, "DOCKER_INSECURE"), {},
                            _ca(env, "DOCKER")))
    return services


def refresh_seconds(env: dict) -> int:
    try:
        value = int((env.get("REFRESH_S") or DEFAULT_REFRESH_S))
    except ValueError:
        return DEFAULT_REFRESH_S
    return max(MIN_REFRESH_S, min(MAX_REFRESH_S, value))

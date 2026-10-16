"""Fetch and summarise each homelab service. Parsers are pure (JSON in, Result out)."""

from __future__ import annotations

import math
import time
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum

from config import Service
from httpjson import HttpError, get_json

MAX_ROWS = 40
BAD_HA_STATES = {"unavailable": "n/a", "unknown": "?"}
KUMA_STATUS = {0: ("DOWN", "err"), 1: ("up", "ok"), 2: ("pending", "warn"), 3: ("maint", "warn")}


class Level(Enum):
    OK = "ok"
    WARN = "warn"
    ERR = "err"


class ParseError(ValueError):
    """The service answered, but not in the expected shape."""


@dataclass(frozen=True)
class Row:
    label: str
    value: str
    level: Level = Level.OK

    def __post_init__(self) -> None:  # APIs can send null; the renderer needs strings
        object.__setattr__(self, "label", "?" if self.label is None else str(self.label))
        object.__setattr__(self, "value", "?" if self.value is None else str(self.value))


@dataclass(frozen=True)
class Result:
    kind: str
    title: str
    level: Level
    summary: str
    detail: str = ""
    rows: tuple = ()
    error: str = ""
    fetched_at: float = field(default=0.0, compare=False)


def worst(levels) -> Level:
    levels = list(levels)
    if Level.ERR in levels:
        return Level.ERR
    return Level.WARN if Level.WARN in levels else Level.OK


def fmt_number(raw: str) -> str:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return str(raw)
    if math.isnan(value) or math.isinf(value):
        return str(raw)
    for limit, suffix in ((1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(value) >= limit * 0.9995:
            return f"{value / limit:.3g}{suffix}"
    if value == int(value):
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".") if abs(value) < 1 else f"{value:.3g}"


def _require(condition: bool, what: str) -> None:
    if not condition:
        raise ParseError(f"unexpected {what} response")


# -- parsers ----------------------------------------------------------------------

def parse_ha(states: list) -> Result:
    _require(isinstance(states, list) and all(isinstance(s, dict) for s in states), "Home Assistant")
    rows = []
    for st in states:
        attrs = st.get("attributes") or {}
        state = str(st.get("state", "?"))
        unit = attrs.get("unit_of_measurement") or ""
        bad = state in BAD_HA_STATES
        rows.append(Row(str(attrs.get("friendly_name") or st.get("entity_id", "?")),
                        BAD_HA_STATES[state] if bad else state + unit, Level.WARN if bad else Level.OK))
    ok = sum(1 for r in rows if r.level == Level.OK)
    return Result("ha", "Home Asst", worst(r.level for r in rows), f"{ok}/{len(rows)}",
                  f"{len(rows)} entities", tuple(rows))


def parse_kuma(page: dict, beats: dict) -> Result:
    _require(isinstance(page, dict) and isinstance(beats, dict), "Uptime Kuma")
    heartbeats = beats.get("heartbeatList") or {}
    rows = []
    for group in page.get("publicGroupList") or []:
        for monitor in group.get("monitorList") or []:
            history = heartbeats.get(str(monitor.get("id"))) or []
            last = history[-1].get("status") if history and isinstance(history[-1], dict) else None
            text, level = KUMA_STATUS.get(last, ("no data", "warn"))
            rows.append(Row(str(monitor.get("name", "?")), text, Level(level)))
    _require(bool(rows) or "publicGroupList" in page, "Uptime Kuma")
    up = sum(1 for r in rows if r.value == "up")
    return Result("kuma", "Uptime", worst(r.level for r in rows), f"{up}/{len(rows)} up",
                  f"{len(rows)} monitors", tuple(rows))


def parse_pve(resources: dict) -> Result:
    data = resources.get("data") if isinstance(resources, dict) else None
    _require(isinstance(data, list), "Proxmox")
    nodes = [r for r in data if isinstance(r, dict) and r.get("type") == "node"]
    guests = [r for r in data if isinstance(r, dict) and r.get("type") in ("qemu", "lxc") and not r.get("template")]
    if not nodes:  # a privilege-separated token without ACLs sees nothing (no 403)
        return Result("pve", "Proxmox", Level.WARN, "0 nodes", "check token ACL", ())
    rows = [Row(n.get("node", "?"), n.get("status", "?"), Level.OK if n.get("status") == "online" else Level.ERR)
            for n in nodes]
    rows += [Row(f"{g.get('vmid', '?')} {g.get('name', '')}".strip(), g.get("status", "?"),
                 Level.OK if g.get("status") == "running" else Level.WARN)
             for g in sorted(guests, key=lambda g: (g.get("status") == "running", g.get("vmid", 0)))]
    online = sum(1 for n in nodes if n.get("status") == "online")
    running = sum(1 for g in guests if g.get("status") == "running")
    level = Level.ERR if online < len(nodes) else Level.OK
    return Result("pve", "Proxmox", level, f"{online}/{len(nodes)} nodes",
                  f"{running}/{len(guests)} guests", tuple(rows[:MAX_ROWS]))


def _prom_value(response: dict) -> str:
    _require(isinstance(response, dict) and response.get("status") == "success", "Prometheus")
    data = response.get("data") or {}
    result = data.get("result")
    if data.get("resultType") in ("scalar", "string") and isinstance(result, list) and len(result) == 2:
        return fmt_number(result[1]) if data["resultType"] == "scalar" else str(result[1])
    _require(isinstance(result, list), "Prometheus")
    if not result:
        return "-"
    if len(result) > 1:
        return f"{len(result)} series"
    return fmt_number(result[0].get("value", [0, "?"])[1])


def parse_prom(labelled: list) -> Result:
    rows = [Row(label, _prom_value(resp)) for label, resp in labelled]
    first = rows[0].value if rows else "-"
    return Result("prom", "Prometheus", Level.OK, first, rows[0].label if rows else "", tuple(rows))


def _ready(node: dict) -> bool:
    for cond in (node.get("status") or {}).get("conditions") or []:
        if cond.get("type") == "Ready":
            return cond.get("status") == "True"
    return False


def parse_k8s(nodes: dict, all_pods: dict, problems: dict) -> Result:
    """all_pods: an *unfiltered* limit=1 list (selectors drop remainingItemCount)."""
    _require(all(isinstance(x, dict) and isinstance(x.get("items"), list) for x in (nodes, all_pods, problems)),
             "Kubernetes")
    rows = [Row(n["metadata"].get("name", "?"), "Ready" if _ready(n) else "NotReady",
                Level.OK if _ready(n) else Level.ERR) for n in nodes["items"]]
    for pod in problems["items"][:MAX_ROWS]:
        meta = pod.get("metadata") or {}
        rows.append(Row(f"{meta.get('namespace', '')}/{meta.get('name', '?')}",
                        (pod.get("status") or {}).get("phase", "?"), Level.WARN))
    ready = sum(1 for n in nodes["items"] if _ready(n))
    pod_count = len(all_pods["items"]) + int((all_pods.get("metadata") or {}).get("remainingItemCount") or 0)
    level = worst([Level.ERR if ready < len(nodes["items"]) else Level.OK,
                   Level.WARN if problems["items"] else Level.OK])
    more = "+" if (problems.get("metadata") or {}).get("continue") else ""
    issues = len(problems["items"])
    return Result("k8s", "Kubernetes", level, f"{ready}/{len(nodes['items'])} nodes",
                  f"{pod_count} pods" + (f", {issues}{more} bad" if issues else ""), tuple(rows))


def parse_docker(containers: list) -> Result:
    _require(isinstance(containers, list), "Docker")
    rows = []
    for c in containers:
        name = ((c.get("Names") or ["?"])[0]).lstrip("/")
        status = c.get("Status", "")
        if c.get("State") != "running":
            rows.append(Row(name, c.get("State", "?"), Level.WARN))
        elif "(unhealthy)" in status:
            rows.append(Row(name, "unhealthy", Level.ERR))
        else:
            rows.append(Row(name, "healthy" if "(healthy)" in status else "running"))
    rows.sort(key=lambda r: (r.level == Level.OK, r.label))
    up = sum(1 for c in containers if c.get("State") == "running")
    unhealthy = sum(1 for r in rows if r.level == Level.ERR)
    return Result("docker", "Docker", worst(r.level for r in rows), f"{up}/{len(containers)} up",
                  f"{unhealthy} unhealthy" if unhealthy else f"{len(containers)} containers", tuple(rows[:MAX_ROWS]))


# -- fetchers ---------------------------------------------------------------------

def _q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _ha_state(get, entity: str, bearer: dict) -> dict:
    try:
        return get(f"/api/states/{_q(entity)}", bearer)
    except HttpError as exc:
        if exc.status == 404:
            raise HttpError(f"Home Asst: {entity} not found", 404) from None
        raise


def _fetch_raw(svc: Service) -> Result:
    def get(path, headers=None):
        return get_json(svc.url + path, svc.title, headers, svc.verify_tls, svc.ca_file)

    bearer = {"Authorization": f"Bearer {svc.token}"} if svc.token else None
    if svc.kind == "ha":
        return parse_ha([_ha_state(get, e, bearer) for e in svc.options["entities"]])
    if svc.kind == "kuma":
        slug = _q(svc.options["slug"])
        return parse_kuma(get(f"/api/status-page/{slug}"), get(f"/api/status-page/heartbeat/{slug}"))
    if svc.kind == "pve":
        return parse_pve(get("/api2/json/cluster/resources", {"Authorization": f"PVEAPIToken={svc.token}"}))
    if svc.kind == "prom":
        return parse_prom([(label, get(f"/api/v1/query?query={_q(q)}", bearer))
                           for label, q in svc.options["queries"]])
    if svc.kind == "k8s":
        # Small responses only: total pod count from an UNFILTERED limit=1 list
        # (remainingItemCount is omitted when a selector is used), plus problem pods.
        problems = _q("status.phase!=Running,status.phase!=Succeeded")
        return parse_k8s(get("/api/v1/nodes", bearer),
                         get("/api/v1/pods?limit=1", bearer),
                         get(f"/api/v1/pods?limit=20&fieldSelector={problems}", bearer))
    if svc.kind == "docker":
        return parse_docker(get("/containers/json?all=1"))
    raise ParseError(f"unknown service {svc.kind}")


def fetch(svc: Service) -> Result:
    """Never raises: failures become an ERR Result carrying a short message."""
    try:
        result = _fetch_raw(svc)
    except (HttpError, ParseError) as exc:
        result = Result(svc.kind, svc.title, Level.ERR, "error", str(exc), (), str(exc))
    except Exception as exc:  # malformed data in an unexpected place; never echo details (may hold headers)
        result = Result(svc.kind, svc.title, Level.ERR, "error", "unexpected data", (),
                        f"{svc.title}: unexpected data ({type(exc).__name__})")
    return Result(**{**result.__dict__, "fetched_at": time.monotonic()})

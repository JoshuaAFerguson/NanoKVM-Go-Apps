"""Local model servers: Ollama, LM Studio, vLLM, llama.cpp (parsers pure)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from httpjson import HttpError, get_json, get_text

METRIC_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eEinfNa]+)")


@dataclass(frozen=True)
class ServerStatus:
    name: str
    summary: str
    detail: str = ""
    rows: list = field(default_factory=list)
    error: str = ""


def _gb(n) -> str:
    try:
        return f"{float(n) / 1e9:.1f}G"
    except (TypeError, ValueError):
        return "?"


def parse_ollama(ps, version) -> ServerStatus:
    models = ps.get("models") if isinstance(ps, dict) else None
    if not isinstance(models, list):
        raise ValueError("unexpected Ollama /api/ps")
    rows = [(str(m.get("name", "?"))[:24], f"{_gb(m.get('size_vram'))} VRAM") for m in models if isinstance(m, dict)]
    v = version.get("version", "") if isinstance(version, dict) else ""
    return ServerStatus("Ollama", f"{len(rows)} loaded", f"v{v}" if v else "", rows)


def parse_lmstudio(data) -> ServerStatus:
    models = data.get("data") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise ValueError("unexpected LM Studio models list")
    loaded = [m for m in models if isinstance(m, dict) and m.get("state") == "loaded"]
    rows = [(str(m.get("id", "?"))[:24], str(m.get("type", ""))) for m in loaded]
    return ServerStatus("LM Studio", f"{len(rows)} loaded", f"{len(models)} available", rows)


def parse_prometheus(text: str) -> dict[str, float]:
    """Metric name -> value summed across label sets (comments/garbage ignored)."""
    metrics: dict[str, float] = {}
    for line in text.splitlines():
        match = METRIC_RE.match(line.strip())
        if not match:
            continue
        try:
            value = float(match.group(3))
        except ValueError:
            continue
        if not math.isfinite(value):  # NaN/Inf would poison sums and break status.json
            continue
        metrics[match.group(1)] = metrics.get(match.group(1), 0.0) + value
    return metrics


class RateTracker:
    """Turns a monotonically increasing counter into a per-second rate."""

    def __init__(self) -> None:
        self.last: tuple[float, float] | None = None

    def rate(self, value: float, now: float) -> float | None:
        previous, self.last = self.last, (value, now)
        if previous is None or now <= previous[1] or value < previous[0]:
            return None  # first sample, or the server restarted
        return (value - previous[0]) / (now - previous[1])


def parse_vllm(metrics: dict, rate: RateTracker, now: float) -> ServerStatus:
    running = metrics.get("vllm:num_requests_running", 0)
    waiting = metrics.get("vllm:num_requests_waiting", 0)
    kv = metrics.get("vllm:kv_cache_usage_perc", metrics.get("vllm:gpu_cache_usage_perc"))
    tps = rate.rate(metrics.get("vllm:generation_tokens_total", 0), now)
    rows = [("Running", f"{running:.0f}"), ("Waiting", f"{waiting:.0f}")]
    if kv is not None:
        rows.append(("KV cache", f"{kv * 100:.0f}%"))
    detail = f"{tps:.0f} tok/s" if tps is not None else "measuring..."
    return ServerStatus("vLLM", f"{running:.0f} running", detail, rows)


def parse_llamacpp(metrics: dict, health) -> ServerStatus:
    busy = metrics.get("llamacpp:requests_processing", 0)
    deferred = metrics.get("llamacpp:requests_deferred", 0)
    tps = metrics.get("llamacpp:predicted_tokens_seconds")
    status = health.get("status", "?") if isinstance(health, dict) else "?"
    rows = [("Processing", f"{busy:.0f}"), ("Queued", f"{deferred:.0f}"), ("Health", status)]
    return ServerStatus("llama.cpp", f"{busy:.0f} busy", f"{tps:.0f} tok/s" if tps is not None else status, rows)


class LocalServers:
    def __init__(self) -> None:
        self.vllm_rate = RateTracker()

    def poll(self, env: dict, now: float) -> list[ServerStatus]:
        out = []
        for key, name in (("OLLAMA_URL", "Ollama"), ("LMSTUDIO_URL", "LM Studio"),
                          ("VLLM_URL", "vLLM"), ("LLAMACPP_URL", "llama.cpp")):
            url = (env.get(key) or "").strip().rstrip("/")
            if not url:
                continue
            try:
                if not url.lower().startswith(("http://", "https://")):
                    raise ValueError(f"{name}: URL must start with http:// or https://")
                if key == "OLLAMA_URL":
                    try:
                        version = get_json(url + "/api/version", name)
                    except HttpError:
                        version = {}  # optional: the model list is what matters
                    out.append(parse_ollama(get_json(url + "/api/ps", name), version))
                elif key == "LMSTUDIO_URL":
                    out.append(parse_lmstudio(get_json(url + "/api/v0/models", name)))
                elif key == "VLLM_URL":
                    out.append(parse_vllm(parse_prometheus(get_text(url + "/metrics", name)), self.vllm_rate, now))
                else:
                    health = get_json(url + "/health", name)
                    out.append(parse_llamacpp(parse_prometheus(get_text(url + "/metrics", name)), health))
            except (HttpError, ValueError) as exc:
                out.append(ServerStatus(name, "down", error=str(exc)[:80]))
        return out

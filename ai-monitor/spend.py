"""AI API spend: Anthropic (Admin API cost report), OpenAI (organization costs),
OpenRouter (key usage). Parsers are pure; fetchers use the bounded shared client.

Units differ per provider (from their docs): Anthropic cost amounts are decimal
strings in *cents*; OpenAI amount.value is a float in *dollars*; OpenRouter
usage fields are floats in dollars.
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass

from httpjson import HttpError, get_json

ANTHROPIC_URL = "https://api.anthropic.com/v1/organizations/cost_report"
OPENAI_URL = "https://api.openai.com/v1/organization/costs"
OPENROUTER_URL = "https://openrouter.ai/api/v1/key"


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class Spend:
    provider: str
    today_usd: float
    month_usd: float
    detail: str = ""
    error: str = ""


def money(usd: float) -> str:
    if usd < 0:
        return "-" + money(-usd)
    if usd >= 1000:
        return f"${usd / 1000:.1f}k"
    if usd == 0:
        return "$0"
    return f"${usd:.2f}"


def _float(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SourceError("bad amount") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise SourceError("bad amount")
    return number


def parse_anthropic(report, today: str) -> Spend:
    buckets = report.get("data") if isinstance(report, dict) else None
    if not isinstance(buckets, list):
        raise SourceError("unexpected Anthropic cost report")
    month = today_total = 0.0
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        cents = sum(_float(r.get("amount")) for r in bucket.get("results") or [] if isinstance(r, dict))
        month += cents / 100
        if str(bucket.get("starting_at", "")).startswith(today):
            today_total += cents / 100
    return Spend("Anthropic", today_total, month)


def parse_openai(page, today_start: int) -> Spend:
    buckets = page.get("data") if isinstance(page, dict) else None
    if not isinstance(buckets, list):
        raise SourceError("unexpected OpenAI costs response")
    month = today_total = 0.0
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        dollars = sum(_float((r.get("amount") or {}).get("value")) for r in bucket.get("results") or []
                      if isinstance(r, dict) and isinstance(r.get("amount"), dict))
        month += dollars
        if bucket.get("start_time") == today_start:
            today_total += dollars
    return Spend("OpenAI", today_total, month)


def parse_openrouter(reply) -> Spend:
    data = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(data, dict) or "usage_monthly" not in data:
        raise SourceError("unexpected OpenRouter key response")
    remaining = data.get("limit_remaining")
    detail = f"{money(max(0.0, _float(remaining)))} left" if remaining is not None else "no limit"
    return Spend("OpenRouter", _float(data.get("usage_daily") or 0), _float(data.get("usage_monthly") or 0), detail)


def _month_start(now: float) -> time.struct_time:
    t = time.gmtime(now)
    return time.struct_time((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, 0))


def fetch_anthropic(key: str, now: float) -> Spend:
    start = time.strftime("%Y-%m-%dT00:00:00Z", _month_start(now))
    report = get_json(f"{ANTHROPIC_URL}?starting_at={start}&bucket_width=1d&limit=31", "Anthropic",
                      {"x-api-key": key, "anthropic-version": "2023-06-01"})
    return parse_anthropic(report, time.strftime("%Y-%m-%d", time.gmtime(now)))


def fetch_openai(key: str, now: float) -> Spend:
    start = calendar.timegm(_month_start(now))  # UTC midnight on the 1st
    today_start = int(now // 86400 * 86400)
    page = get_json(f"{OPENAI_URL}?start_time={start}&bucket_width=1d&limit=31", "OpenAI",
                    {"Authorization": f"Bearer {key}"})
    return parse_openai(page, today_start)


def fetch_openrouter(key: str, now: float) -> Spend:
    return parse_openrouter(get_json(OPENROUTER_URL, "OpenRouter", {"Authorization": f"Bearer {key}"}))


FETCHERS = {"ANTHROPIC_ADMIN_KEY": fetch_anthropic, "OPENAI_ADMIN_KEY": fetch_openai,
            "OPENROUTER_KEY": fetch_openrouter}


def fetch_all(env: dict, now: float) -> list[Spend]:
    out = []
    for key_name, fetch in FETCHERS.items():
        key = (env.get(key_name) or "").strip()
        if not key:
            continue
        name = {"ANTHROPIC_ADMIN_KEY": "Anthropic", "OPENAI_ADMIN_KEY": "OpenAI", "OPENROUTER_KEY": "OpenRouter"}[key_name]
        try:
            out.append(fetch(key, now))
        except (HttpError, SourceError) as exc:
            out.append(Spend(name, 0.0, 0.0, error=str(exc)[:80]))
    return out

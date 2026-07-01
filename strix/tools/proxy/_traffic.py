"""Pure traffic-digest helpers for the ``traffic_health`` tool.

Kept in a sibling module so the branch-heavy ``summarize_traffic`` does not
trip ruff's PLR0912/PLR0915 on ``tools.py`` (whose per-file ignore is
deliberately narrow). ``tools.py`` re-exports ``summarize_traffic``.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any


# Status codes treated as an active block (WAF / rate-limit / unavailable).
_BLOCK_CODES = (403, 429, 503)

# Heuristic thresholds (flat MVP thresholds — see spec 02-traffic-health.md).
_BLOCK_RATE_WAF = 0.30
_RATE_LIMIT_THROTTLE = 0.10
_SERVER_ERROR_THROTTLE = 0.20

_MAX_HOSTS = 10

_ADVICE_EMPTY = "No traffic in window — nothing to assess yet."
_ADVICE_WAF_AND_THROTTLE = (
    "High 403/429 rate suggests a WAF and throttling — lower request rate and "
    "threads, add backoff, and switch to encoded/evasion payloads before continuing."
)
_ADVICE_WAF = (
    "High block rate with 403s suggests a WAF — switch to encoded/evasion "
    "payloads and slow down before continuing."
)
_ADVICE_THROTTLE = (
    "Throttling detected (429/5xx) — lower request rate and threads and add "
    "backoff before scaling load."
)
_ADVICE_OK = "Traffic looks healthy — no throttling or WAF signs; safe to proceed."


def _percentile(values: list[int], q: int) -> int:
    """Nearest-rank percentile on a non-empty list. No numpy."""
    ordered = sorted(values)
    n = len(ordered)
    index = math.ceil(q / 100 * n) - 1
    index = max(0, min(index, n - 1))
    return ordered[index]


def _status_class(status: int) -> str | None:
    if 200 <= status < 300:
        return "2xx"
    if 300 <= status < 400:
        return "3xx"
    if 400 <= status < 500:
        return "4xx"
    if 500 <= status < 600:
        return "5xx"
    return None


def _latency_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    measured = [row["roundtrip_ms"] for row in rows if row.get("roundtrip_ms")]
    if not measured:
        return {"measured": 0}
    return {
        "p50": _percentile(measured, 50),
        "p95": _percentile(measured, 95),
        "measured": len(measured),
    }


def _by_host(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    host_counts: Counter[str] = Counter(row["host"] for row in rows)
    result: list[dict[str, Any]] = []
    for host, count in host_counts.most_common(_MAX_HOSTS):
        host_rows = [row for row in rows if row["host"] == host]
        responded = [row for row in host_rows if row["status"] is not None]
        blocked = sum(1 for row in responded if row["status"] in _BLOCK_CODES)
        block_rate = round(blocked / len(responded), 2) if responded else 0.0
        statuses = Counter(str(row["status"]) for row in responded)
        top_status = statuses.most_common(1)[0][0] if statuses else ""
        result.append(
            {
                "host": host,
                "count": count,
                "block_rate": block_rate,
                "top_status": top_status,
            }
        )
    return result


def _advice(*, waf_suspected: bool, throttle_recommended: bool) -> str:
    if waf_suspected and throttle_recommended:
        return _ADVICE_WAF_AND_THROTTLE
    if waf_suspected:
        return _ADVICE_WAF
    if throttle_recommended:
        return _ADVICE_THROTTLE
    return _ADVICE_OK


def summarize_traffic(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce mapped proxy rows to the traffic-health digest.

    Each row: ``{"status": int|None, "roundtrip_ms": int|None,
    "host": str, "method": str}``. Pure; no Caido or network access.
    """
    window = len(rows)
    responded_rows = [row for row in rows if row["status"] is not None]
    responded = len(responded_rows)
    no_response = window - responded

    status_histogram: dict[str, int] = {}
    class_counts = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
    for row in responded_rows:
        status = row["status"]
        key = str(status)
        status_histogram[key] = status_histogram.get(key, 0) + 1
        cls = _status_class(status)
        if cls is not None:
            class_counts[cls] += 1

    if responded == 0:
        block_rate = 0.0
        rate_limited_rate = 0.0
        server_error_rate = 0.0
    else:
        blocked = sum(1 for row in responded_rows if row["status"] in _BLOCK_CODES)
        rate_limited = status_histogram.get("429", 0)
        block_rate = round(blocked / responded, 2)
        rate_limited_rate = round(rate_limited / responded, 2)
        server_error_rate = round(class_counts["5xx"] / responded, 2)

    has_403 = status_histogram.get("403", 0) > 0
    waf_suspected = block_rate >= _BLOCK_RATE_WAF and has_403
    throttle_recommended = (
        rate_limited_rate >= _RATE_LIMIT_THROTTLE
        or server_error_rate >= _SERVER_ERROR_THROTTLE
    )

    if window == 0 or responded == 0:
        advice = _ADVICE_EMPTY
        waf_suspected = False
        throttle_recommended = False
    else:
        advice = _advice(
            waf_suspected=waf_suspected, throttle_recommended=throttle_recommended
        )

    return {
        "window": window,
        "responded": responded,
        "no_response": no_response,
        "status_histogram": status_histogram,
        "class_counts": class_counts,
        "block_rate": block_rate,
        "rate_limited_rate": rate_limited_rate,
        "server_error_rate": server_error_rate,
        "latency_ms": _latency_summary(rows),
        "by_host": _by_host(rows),
        "waf_suspected": waf_suspected,
        "throttle_recommended": throttle_recommended,
        "advice": advice,
    }

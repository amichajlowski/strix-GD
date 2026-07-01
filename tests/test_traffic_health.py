"""Unit tests for the `traffic_health` pure digest helper and tool wrapper.

`summarize_traffic` is the pure, unit-tested target (no Caido, no network).
The `traffic_health` `@function_tool` wrapper tests exercise the I/O mapping
with a fake Caido connection object, per `tests/test_agents_graph.py`'s
`_FakeSession` style (plain attribute-access objects, not dicts).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from agents.tool_context import ToolContext

from strix.tools.proxy import tools as traffic_tools
from strix.tools.proxy.tools import summarize_traffic, traffic_health


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #


def _row(
    status: int | None = 200,
    roundtrip_ms: int | None = None,
    host: str = "api.XXXX.example",
    method: str = "GET",
) -> dict[str, Any]:
    return {"status": status, "roundtrip_ms": roundtrip_ms, "host": host, "method": method}


def _tc(inner: dict[str, Any], args: str = "{}") -> ToolContext:
    return ToolContext(
        context=inner, tool_name="traffic_health", tool_call_id="t1", tool_arguments=args
    )


# --------------------------------------------------------------------------- #
# 1. Empty window
# --------------------------------------------------------------------------- #


def test_summarize_empty_window_is_safe() -> None:
    digest = summarize_traffic([])

    assert digest["window"] == 0
    assert digest["responded"] == 0
    assert digest["no_response"] == 0
    assert digest["status_histogram"] == {}
    assert digest["class_counts"] == {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
    assert digest["block_rate"] == 0
    assert digest["rate_limited_rate"] == 0
    assert digest["server_error_rate"] == 0
    assert digest["latency_ms"] == {"measured": 0}
    assert digest["by_host"] == []
    assert digest["waf_suspected"] is False
    assert digest["throttle_recommended"] is False
    assert digest["advice"] == "No traffic in window — nothing to assess yet."


# --------------------------------------------------------------------------- #
# 2. Histogram + class counts
# --------------------------------------------------------------------------- #


def test_status_histogram_and_class_counts() -> None:
    rows = (
        [_row(status=200)] * 40
        + [_row(status=301)] * 5
        + [_row(status=403)] * 30
        + [_row(status=429)] * 15
        + [_row(status=500)] * 2
    )

    digest = summarize_traffic(rows)

    assert digest["window"] == 92
    assert digest["responded"] == 92
    assert digest["no_response"] == 0
    assert digest["status_histogram"] == {
        "200": 40,
        "301": 5,
        "403": 30,
        "429": 15,
        "500": 2,
    }
    assert digest["class_counts"] == {"2xx": 40, "3xx": 5, "4xx": 45, "5xx": 2}


# --------------------------------------------------------------------------- #
# 3. Throttle recommended on high rate-limit rate
# --------------------------------------------------------------------------- #


def test_rate_limited_detection_sets_throttle() -> None:
    rows = [_row(status=200)] * 80 + [_row(status=429)] * 20

    digest = summarize_traffic(rows)

    assert digest["rate_limited_rate"] == pytest.approx(0.20)
    assert digest["throttle_recommended"] is True
    assert "lower" in digest["advice"].lower() or "rate" in digest["advice"].lower()


# --------------------------------------------------------------------------- #
# 4. WAF suspected on high block rate with 403 present
# --------------------------------------------------------------------------- #


def test_waf_suspected_on_high_block_rate() -> None:
    rows = [_row(status=200)] * 60 + [_row(status=403)] * 40

    digest = summarize_traffic(rows)

    assert digest["block_rate"] == pytest.approx(0.40)
    assert digest["waf_suspected"] is True


# --------------------------------------------------------------------------- #
# 5. No false positive on healthy traffic
# --------------------------------------------------------------------------- #


def test_no_false_waf_on_healthy_traffic() -> None:
    rows = [_row(status=200)] * 95 + [_row(status=301)] * 5

    digest = summarize_traffic(rows)

    assert digest["waf_suspected"] is False
    assert digest["throttle_recommended"] is False


# --------------------------------------------------------------------------- #
# 6. Latency percentiles only when measured
# --------------------------------------------------------------------------- #


def test_latency_percentiles_present_only_when_measured() -> None:
    measured_rows = [_row(status=200, roundtrip_ms=ms) for ms in range(1, 61)]  # 1..60 ms

    digest = summarize_traffic(measured_rows)

    assert digest["latency_ms"]["measured"] == 60
    assert "p50" in digest["latency_ms"]
    assert "p95" in digest["latency_ms"]

    unmeasured_rows = [_row(status=200, roundtrip_ms=None)] * 10 + [
        _row(status=200, roundtrip_ms=0)
    ] * 10

    digest_absent = summarize_traffic(unmeasured_rows)

    assert digest_absent["latency_ms"] == {"measured": 0}
    assert "p50" not in digest_absent["latency_ms"]
    assert "p95" not in digest_absent["latency_ms"]


# --------------------------------------------------------------------------- #
# 7. by_host capped and sorted
# --------------------------------------------------------------------------- #


def test_by_host_capped_and_sorted() -> None:
    rows = []
    for i in range(15):
        host = f"host{i:02d}.XXXX.example"
        # Give each host a distinct, descending count so sort order is unambiguous.
        count = 20 - i
        rows.extend([_row(status=200, host=host)] * count)

    digest = summarize_traffic(rows)

    assert len(digest["by_host"]) == 10
    counts = [entry["count"] for entry in digest["by_host"]]
    assert counts == sorted(counts, reverse=True)
    assert digest["by_host"][0]["host"] == "host00.XXXX.example"
    assert digest["by_host"][0]["count"] == 20


# --------------------------------------------------------------------------- #
# 8. Rows without a response are counted as no_response, not in rate fields
# --------------------------------------------------------------------------- #


def test_rows_without_response_counted_as_no_response() -> None:
    rows = [_row(status=200)] * 10 + [_row(status=None)] * 5

    digest = summarize_traffic(rows)

    assert digest["window"] == 15
    assert digest["responded"] == 10
    assert digest["no_response"] == 5
    assert digest["block_rate"] == 0
    assert digest["rate_limited_rate"] == 0
    assert digest["server_error_rate"] == 0
    assert digest["status_histogram"] == {"200": 10}


# --------------------------------------------------------------------------- #
# 9. Tool wrapper: no client bound
# --------------------------------------------------------------------------- #


async def test_traffic_health_tool_returns_no_client_without_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(traffic_tools, "_ctx_client", lambda _ctx: None)

    ctx = _tc({})
    out = json.loads(await traffic_health.on_invoke_tool(ctx, "{}"))

    assert out["success"] is False
    assert "caido" in out["error"].lower() or "client" in out["error"].lower()


# --------------------------------------------------------------------------- #
# 10. Tool wrapper: maps Caido edges, drops query strings
# --------------------------------------------------------------------------- #


@dataclass
class _FakeRequest:
    id: str
    host: str
    port: int
    method: str
    path: str
    query: str
    is_tls: bool
    created_at: datetime


@dataclass
class _FakeResponse:
    id: str
    status_code: int
    length: int
    created_at: datetime
    roundtrip_time: int


@dataclass
class _FakeNode:
    request: _FakeRequest
    response: _FakeResponse | None


@dataclass
class _FakeEdge:
    cursor: str
    node: _FakeNode


@dataclass
class _FakePageInfo:
    has_next_page: bool
    has_previous_page: bool
    start_cursor: str | None
    end_cursor: str | None


@dataclass
class _FakeConnection:
    edges: list[_FakeEdge]
    page_info: _FakePageInfo


_SECRET_QUERY = "token=XXXX-super-secret"  # noqa: S105  # test placeholder, not a real secret


def _fake_edge(
    *,
    cursor: str,
    host: str,
    status_code: int,
    query: str = "",
    roundtrip_time: int = 0,
) -> _FakeEdge:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    req = _FakeRequest(
        id=f"req-{cursor}",
        host=host,
        port=443,
        method="GET",
        path="/api/XXXX",
        query=query,
        is_tls=True,
        created_at=now,
    )
    resp = _FakeResponse(
        id=f"resp-{cursor}",
        status_code=status_code,
        length=100,
        created_at=now,
        roundtrip_time=roundtrip_time,
    )
    return _FakeEdge(cursor=cursor, node=_FakeNode(request=req, response=resp))


def _fake_edge_no_response(*, cursor: str, host: str) -> _FakeEdge:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    req = _FakeRequest(
        id=f"req-{cursor}",
        host=host,
        port=443,
        method="GET",
        path="/api/XXXX",
        query="",
        is_tls=True,
        created_at=now,
    )
    return _FakeEdge(cursor=cursor, node=_FakeNode(request=req, response=None))


async def test_traffic_health_tool_maps_caido_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    edges = [
        _fake_edge(cursor="1", host="api.XXXX.example", status_code=200, query=_SECRET_QUERY),
        _fake_edge(cursor="2", host="api.XXXX.example", status_code=200),
        _fake_edge(cursor="3", host="api.XXXX.example", status_code=403),
        _fake_edge_no_response(cursor="4", host="api.XXXX.example"),
    ]
    connection = _FakeConnection(
        edges=edges,
        page_info=_FakePageInfo(
            has_next_page=False, has_previous_page=False, start_cursor="1", end_cursor="4"
        ),
    )

    fake_client = object()
    monkeypatch.setattr(traffic_tools, "_ctx_client", lambda _ctx: fake_client)

    async def _fake_list_requests_with_client(*_args: Any, **_kwargs: Any) -> _FakeConnection:
        return connection

    monkeypatch.setattr(
        traffic_tools.caido_api,
        "list_requests_with_client",
        _fake_list_requests_with_client,
    )

    ctx = _tc({"caido_client": fake_client})
    raw_out = await traffic_health.on_invoke_tool(ctx, "{}")
    out = json.loads(raw_out)

    assert out["success"] is True
    assert _SECRET_QUERY not in raw_out
    assert "token" not in raw_out
    assert out["window"] == 4
    assert out["responded"] == 3
    assert out["no_response"] == 1
    assert out["status_histogram"] == {"200": 2, "403": 1}

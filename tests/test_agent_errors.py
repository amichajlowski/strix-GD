"""Tests for structured error recording and secret scrubbing on the coordinator."""

from __future__ import annotations

import pytest

from strix.core.agents import AgentCoordinator
from strix.core.scrubbing import MAX_MESSAGE_LEN


@pytest.mark.parametrize(
    ("raw", "secret", "must_contain"),
    [
        ("Authorization: Bearer abc123def456", "abc123def456", "XXXX"),
        ("Bearer abc123def456 failed", "abc123def456", "XXXX"),
        ("Set-Cookie: session=deadbeefcafe; path=/", "deadbeefcafe", "XXXX"),
        ("Cookie: token=deadbeefcafe", "deadbeefcafe", "XXXX"),
        ("request api_key=supersecretvalue123 rejected", "supersecretvalue123", "XXXX"),
        ('{"password": "hunter2pass"}', "hunter2pass", "XXXX"),
        ('{"secret": "topsecretval"}', "topsecretval", "XXXX"),
        ("credential=mycredvalue99", "mycredvalue99", "XXXX"),
        ("token=mytokenvalue00", "mytokenvalue00", "XXXX"),
        ("connect https://user:sup3rpass@example.test/x", "sup3rpass", "XXXX"),
        ("jwt eyJhbGciOi.eyJzdWIiOiIx.SflKxwRJ here", "SflKxwRJ", "XXXX"),
        ("key AKIAIOSFODNN7EXAMPLE leaked", "AKIAIOSFODNN7EXAMPLE", "XXXX"),
        ("provider key sk-testvalue123 leaked", "sk-testvalue123", "XXXX"),
        ("slack xoxb-1234567890-token leaked", "xoxb-1234567890-token", "XXXX"),
        ("github ghp_1234567890abcdef leaked", "ghp_1234567890abcdef", "XXXX"),
        ("github github_pat_1234567890abcdef leaked", "github_pat_1234567890abcdef", "XXXX"),
    ],
)
async def test_record_error_scrubs_structured_secret_patterns(
    raw: str, secret: str, must_contain: str
) -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("a1", "strix", parent_id=None)

    await coordinator.record_error("a1", RuntimeError(raw))

    last_error = coordinator.metadata["a1"]["last_error"]
    assert last_error["type"] == "RuntimeError"
    assert must_contain in last_error["message"]
    assert secret not in last_error["message"]
    assert len(last_error["message"]) <= MAX_MESSAGE_LEN


async def test_record_error_keeps_benign_path_readable() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("a1", "strix", parent_id=None)

    benign = "Failed to read /workspace/repo/app.py at line 42"
    await coordinator.record_error("a1", FileNotFoundError(benign))

    last_error = coordinator.metadata["a1"]["last_error"]
    assert last_error["message"] == benign
    assert last_error["type"] == "FileNotFoundError"
    assert "occurred_at" in last_error


async def test_record_error_captures_status_code() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("a1", "strix", parent_id=None)

    exc = RuntimeError("upstream 500")
    exc.status_code = 500  # type: ignore[attr-defined]
    await coordinator.record_error("a1", exc)

    assert coordinator.metadata["a1"]["last_error"]["status_code"] == 500


async def test_mark_running_clears_last_error() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("a1", "strix", parent_id=None)
    await coordinator.record_error("a1", RuntimeError("boom"))
    assert "last_error" in coordinator.metadata["a1"]

    await coordinator.mark_running("a1")

    assert "last_error" not in coordinator.metadata["a1"]
    assert coordinator.statuses["a1"] == "running"


async def test_systemic_error_summary_groups_repeated_terminal_errors() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    for index in range(3):
        agent_id = f"child-{index}"
        await coordinator.register(agent_id, f"child {index}", parent_id="root")
        exc = RuntimeError("empty_stream")
        exc.status_code = 500  # type: ignore[attr-defined]
        await coordinator.record_error(agent_id, exc)
        await coordinator.set_status(agent_id, "failed")

    summary = await coordinator.systemic_error_summary(threshold=3)

    assert summary is not None
    assert summary["count"] == 3
    assert summary["error"]["type"] == "RuntimeError"
    assert summary["error"]["message"] == "empty_stream"
    assert summary["error"]["status_code"] == 500
    assert {agent["agent_id"] for agent in summary["agents"]} == {
        "child-0",
        "child-1",
        "child-2",
    }

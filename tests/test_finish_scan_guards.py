"""Regression tests for root completion guardrails."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from strix.core.agents import AgentCoordinator
from strix.tools.finish.tool import _completion_blockers
from strix.tools.todo import tools as todo_tools


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clear_todos(tmp_path: Path) -> None:
    todo_tools.hydrate_todos_from_disk(tmp_path)


async def test_completion_blocks_failed_child_agent() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    await coordinator.register("child", "Enumeration", parent_id="root")
    await coordinator.record_error("child", RuntimeError("empty_stream"))
    await coordinator.set_status("child", "failed")

    blockers = await _completion_blockers(
        {"coordinator": coordinator, "agent_id": "root", "parent_id": None}
    )

    assert blockers["unresolved_agents"][0]["agent_id"] == "child"
    assert blockers["unresolved_agents"][0]["status"] == "failed"
    assert blockers["unresolved_agents"][0]["last_error"]["message"] == "empty_stream"


async def test_completion_blocks_error_stopped_child_agent() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    await coordinator.register("child", "Enumeration", parent_id="root")
    await coordinator.record_error("child", RuntimeError("empty_stream"))
    await coordinator.set_status("child", "stopped")

    blockers = await _completion_blockers(
        {"coordinator": coordinator, "agent_id": "root", "parent_id": None}
    )

    assert blockers["unresolved_agents"][0]["agent_id"] == "child"
    assert blockers["unresolved_agents"][0]["status"] == "stopped"


async def test_completion_allows_cleanly_stopped_child_agent() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    await coordinator.register("child", "Enumeration", parent_id="root")
    await coordinator.set_status("child", "stopped")

    blockers = await _completion_blockers(
        {"coordinator": coordinator, "agent_id": "root", "parent_id": None}
    )

    assert blockers["unresolved_agents"] == []
    assert blockers["unresolved_todos"] == []


async def test_completion_blocks_root_unresolved_todos() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    todo_tools._todos_storage["root"] = {
        "todo-1": {
            "title": "Finish enumeration",
            "priority": "high",
            "status": "pending",
        }
    }

    blockers = await _completion_blockers(
        {"coordinator": coordinator, "agent_id": "root", "parent_id": None}
    )

    assert blockers["unresolved_agents"] == []
    assert blockers["unresolved_todos"][0]["agent_id"] == "root"
    assert blockers["unresolved_todos"][0]["total_unresolved"] == 1
    assert blockers["unresolved_todos"][0]["todos"][0]["title"] == "Finish enumeration"

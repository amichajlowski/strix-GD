"""Tests that a child's terminal error wakes a waiting parent."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from strix.core.agents import AgentCoordinator
from strix.core.execution import _notify_parent_on_terminal_error


class _FakeSession:
    def __init__(self) -> None:
        self.items: list[Any] = []

    async def add_items(self, items: list[Any]) -> None:
        self.items.extend(items)

    async def get_items(self) -> list[Any]:
        return list(self.items)


async def _failed_child_coordinator() -> AgentCoordinator:
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    await coordinator.register("child", "recon", parent_id="root")
    coordinator.runtimes["root"].session = _FakeSession()
    await coordinator.record_error("child", RuntimeError("boom"))
    await coordinator.set_status("child", "failed")
    return coordinator


@pytest.mark.asyncio
async def test_parent_wait_unblocks_on_child_failure_message() -> None:
    coordinator = await _failed_child_coordinator()

    # Parent parked in the interactive coordinator wait (run_agent_loop's outer loop).
    waiter = asyncio.create_task(coordinator.wait_for_message("root"))
    await asyncio.sleep(0)
    assert not waiter.done()

    await _notify_parent_on_terminal_error(coordinator, "child", "failed")

    await asyncio.wait_for(waiter, timeout=1.0)
    assert coordinator.pending_counts["root"] == 1


@pytest.mark.asyncio
async def test_noninteractive_parent_wait_unblocks_on_child_failure_message() -> None:
    coordinator = await _failed_child_coordinator()

    # Mirror the non-interactive wait_for_message tool: a bounded wait on the
    # coordinator. The failure notification must return it well before timeout.
    async def parent_waits() -> None:
        await asyncio.wait_for(coordinator.wait_for_message("root"), timeout=600)

    waiter = asyncio.create_task(parent_waits())
    await asyncio.sleep(0)
    assert not waiter.done()

    await _notify_parent_on_terminal_error(coordinator, "child", "failed")

    await asyncio.wait_for(waiter, timeout=1.0)

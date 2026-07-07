"""Tests for the scan-wide budget-stop signal on the agent coordinator."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import Mock

import pytest

from strix.core.agents import AgentCoordinator
from strix.core.context_limit import ContextLimitFilter


@pytest.mark.asyncio
async def test_budget_stop_sets_flag() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)

    assert coordinator.budget_stopped is False
    await coordinator.trigger_budget_stop()
    assert coordinator.budget_stopped is True


@pytest.mark.asyncio
async def test_budget_stop_unblocks_parked_agent() -> None:
    # A parent parked in wait_for_message (awaiting a child) must be released so
    # it can exit, no matter where in the tree the budget limit was hit.
    coordinator = AgentCoordinator()
    await coordinator.register("parent", "strix", parent_id=None)

    waiter = asyncio.create_task(coordinator.wait_for_message("parent"))
    await asyncio.sleep(0)  # let the waiter park
    assert not waiter.done()

    await coordinator.trigger_budget_stop()
    await asyncio.wait_for(waiter, timeout=1.0)


@pytest.mark.asyncio
async def test_wait_for_message_returns_immediately_after_budget_stop() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("agent", "recon", parent_id="parent")
    await coordinator.trigger_budget_stop()

    # No pending messages, but the stop flag short-circuits the wait.
    await asyncio.wait_for(coordinator.wait_for_message("agent"), timeout=1.0)


class _FakeSession:
    def __init__(self, items: list[Any] | None = None) -> None:
        self.items: list[Any] = list(items or [])

    async def add_items(self, items: list[Any]) -> None:
        self.items.extend(items)

    async def get_items(self) -> list[Any]:
        return list(self.items)

    async def clear_session(self) -> None:
        self.items = []


@pytest.mark.asyncio
async def test_interactive_root_failure_parks_without_reraising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from strix.core import execution

    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("kaboom upstream failure")

    monkeypatch.setattr(execution.Runner, "run_streamed", boom)

    result = await execution._run_cycle(
        object(),
        coordinator,
        "root",
        input_data="task",
        run_config=object(),
        context={"parent_id": None},
        max_turns=5,
        session=None,
        interactive=True,
        event_sink=None,
        hooks=None,
    )

    assert result is None
    assert coordinator.statuses["root"] in {"failed", "crashed"}
    assert "last_error" in coordinator.metadata["root"]


@pytest.mark.asyncio
async def test_failed_child_notifies_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    from strix.core import execution

    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    await coordinator.register("child", "recon", parent_id="root")
    coordinator.runtimes["root"].session = _FakeSession()  # parent inbox

    from agents.exceptions import UserError

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise UserError("bad child input api_key=supersecretvalue")

    monkeypatch.setattr(execution.Runner, "run_streamed", boom)

    await execution._run_cycle(
        object(),
        coordinator,
        "child",
        input_data="task",
        run_config=object(),
        context={"parent_id": "root"},
        max_turns=5,
        session=None,
        interactive=True,
        event_sink=None,
        hooks=None,
    )

    assert coordinator.statuses["child"] == "failed"
    assert coordinator.pending_counts["root"] == 1
    parent_msg = coordinator.runtimes["root"].session.items[-1]
    text = parent_msg["content"]
    assert "child" in text
    assert "supersecretvalue" not in text  # scrubbed


@pytest.mark.asyncio
async def test_user_stopped_child_does_not_emit_error_message() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    await coordinator.register("child", "recon", parent_id="root")
    coordinator.runtimes["root"].session = _FakeSession()

    from strix.core.execution import _notify_parent_on_terminal_error

    # Graceful user stop: no last_error attached.
    await coordinator.set_status("child", "stopped")
    await _notify_parent_on_terminal_error(coordinator, "child", "stopped")

    assert coordinator.pending_counts.get("root", 0) == 0
    assert coordinator.runtimes["root"].session.items == []


async def _run_child_cycle(
    monkeypatch: pytest.MonkeyPatch,
    coordinator: AgentCoordinator,
    exc: BaseException,
) -> None:
    from strix.core import execution

    def boom(*_a: Any, **_k: Any) -> Any:
        raise exc

    monkeypatch.setattr(execution.Runner, "run_streamed", boom)
    await execution._run_cycle(
        object(),
        coordinator,
        "child",
        input_data="task",
        run_config=object(),
        context={"parent_id": "root"},
        max_turns=5,
        session=None,
        interactive=True,
        event_sink=None,
        hooks=None,
    )


@pytest.mark.asyncio
async def test_crashed_child_notifies_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    await coordinator.register("child", "recon", parent_id="root")
    coordinator.runtimes["root"].session = _FakeSession()

    await _run_child_cycle(monkeypatch, coordinator, RuntimeError("unexpected blowup"))

    assert coordinator.statuses["child"] == "crashed"
    assert coordinator.pending_counts["root"] == 1
    content = coordinator.runtimes["root"].session.items[-1]["content"]
    assert "type=terminal_error" in content
    assert "crashed" in content


@pytest.mark.asyncio
async def test_max_turns_stopped_child_notifies_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.exceptions import MaxTurnsExceeded

    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    await coordinator.register("child", "recon", parent_id="root")
    coordinator.runtimes["root"].session = _FakeSession()

    await _run_child_cycle(monkeypatch, coordinator, MaxTurnsExceeded("max turns"))

    assert coordinator.statuses["child"] == "stopped"
    assert "last_error" in coordinator.metadata["child"]
    assert coordinator.pending_counts["root"] == 1


@pytest.mark.asyncio
async def test_child_loop_exception_is_caught_and_recorded(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from strix.core import execution

    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    await coordinator.register("child", "recon", parent_id="root")
    coordinator.runtimes["root"].session = _FakeSession()

    async def boom_loop(**_k: Any) -> None:
        raise RuntimeError("child loop blew up")

    monkeypatch.setattr(execution, "run_agent_loop", boom_loop)

    await execution._start_child_runner(
        parent_ctx={"agent_id": "root"},
        coordinator=coordinator,
        agents_db_path=tmp_path / "agents.db",
        sessions_to_close=[],
        run_config=object(),
        max_turns=5,
        interactive=True,
        child_agent=object(),
        child_id="child",
        name="recon",
        parent_id="root",
        task="t",
        initial_input=[],
    )
    task = coordinator.runtimes["child"].task
    assert task is not None
    await task  # must not raise an unhandled task exception

    assert coordinator.statuses["child"] == "crashed"
    assert "last_error" in coordinator.metadata["child"]
    assert coordinator.pending_counts["root"] == 1


@pytest.mark.asyncio
async def test_budget_stop_does_not_emit_child_error_notification(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from strix.core import execution
    from strix.core.hooks import BudgetExceededError

    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    await coordinator.register("child", "recon", parent_id="root")
    coordinator.runtimes["root"].session = _FakeSession()

    async def budget_loop(**_k: Any) -> None:
        raise BudgetExceededError("scan budget reached")

    monkeypatch.setattr(execution, "run_agent_loop", budget_loop)

    await execution._start_child_runner(
        parent_ctx={"agent_id": "root"},
        coordinator=coordinator,
        agents_db_path=tmp_path / "agents.db",
        sessions_to_close=[],
        run_config=object(),
        max_turns=5,
        interactive=True,
        child_agent=object(),
        child_id="child",
        name="recon",
        parent_id="root",
        task="t",
        initial_input=[],
    )
    task = coordinator.runtimes["child"].task
    assert task is not None
    await task

    # Budget stop is a clean scan-wide shutdown, not a child failure.
    assert coordinator.pending_counts.get("root", 0) == 0
    assert coordinator.runtimes["root"].session.items == []


@pytest.mark.asyncio
async def test_repair_malformed_tool_calls_neutralises_bad_arguments() -> None:
    from strix.core.sessions import repair_malformed_tool_calls_in_session

    session = _FakeSession(
        [
            {"type": "message", "role": "assistant", "content": "planning"},
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "create_agent",
                "arguments": '{"task": "do x"',
            },  # malformed: missing closing brace
            {
                "type": "function_call",
                "call_id": "c2",
                "name": "noop",
                "arguments": '{"ok": true}',
            },  # valid
        ]
    )

    repaired = await repair_malformed_tool_calls_in_session(session)  # type: ignore[arg-type]

    assert repaired is True
    assert session.items[1]["arguments"] == "{}"  # neutralised
    assert session.items[2]["arguments"] == '{"ok": true}'  # valid one untouched
    assert session.items[0]["content"] == "planning"  # non-tool item untouched
    # Idempotent: a clean session repairs nothing.
    assert await repair_malformed_tool_calls_in_session(session) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_run_cycle_repairs_poisoned_history_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 400 whose cause is a malformed tool call already in history must trigger
    # a session repair + retry, not immediately kill the agent.
    from strix.core import execution

    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    session = _FakeSession(
        [
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "create_agent",
                "arguments": '{"task": "do x"',
            },  # the poison
        ]
    )

    class _RejectedError(Exception):
        status_code = 400

    calls = {"n": 0}

    def flaky(*_args: Any, **_kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _RejectedError("Expecting ',' delimiter: line 1 column 32 (char 31)")
        raise RuntimeError("second-attempt sentinel")  # distinct: proves we retried

    monkeypatch.setattr(execution.Runner, "run_streamed", flaky)

    result = await execution._run_cycle(
        object(),
        coordinator,
        "root",
        input_data="task",
        run_config=object(),
        context={"parent_id": None},
        max_turns=5,
        session=session,
        interactive=True,
        event_sink=None,
        hooks=None,
    )

    assert result is None
    assert calls["n"] == 2  # retried after repairing history
    assert session.items[0]["arguments"] == "{}"  # poison neutralised
    assert coordinator.statuses["root"] in {"failed", "crashed"}


class _FakeStream:
    def __init__(self) -> None:
        self.run_loop_exception: BaseException | None = None

    async def stream_events(self) -> Any:
        return
        yield  # pragma: no cover - makes this an async generator with 0 events


class _FakeRunConfig:
    def __init__(self, call_model_input_filter: Any) -> None:
        self.call_model_input_filter = call_model_input_filter


@pytest.mark.asyncio
async def test_double_context_400_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Reproduces the observed `_6ed5` failure: the provider rejects the request
    # twice with the *same* reported max (262144 == the configured window), so
    # note_context_length can't lower anything on the second rejection. The
    # agent must shrink its budget and recover instead of parking as failed.
    from strix.core import execution

    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)

    context_filter = ContextLimitFilter(configured_window=262_144)
    shrink_spy = Mock(wraps=context_filter.shrink)
    context_filter.shrink = shrink_spy  # type: ignore[method-assign]
    run_config = _FakeRunConfig(context_filter)

    class _RejectedError(Exception):
        status_code = 400

    message = (
        "Error code: 400 - This model's maximum context length is 262144 tokens. "
        "However, your prompt contains at least 262145 input tokens..."
    )

    calls = {"n": 0}

    def flaky(*_args: Any, **_kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _RejectedError(message)
        return _FakeStream()

    monkeypatch.setattr(execution.Runner, "run_streamed", flaky)

    result = await execution._run_cycle(
        object(),
        coordinator,
        "root",
        input_data="task",
        run_config=run_config,
        context={"parent_id": None},
        max_turns=5,
        session=None,
        interactive=True,
        event_sink=None,
        hooks=None,
    )

    assert calls["n"] == 3  # two rejections, then the recovered attempt succeeded
    assert result is not None
    # The regression: previously this parked the agent as failed after the
    # second identical-max 400. It must now recover.
    assert coordinator.statuses["root"] not in {"failed", "crashed"}
    shrink_spy.assert_called_once_with(execution._CONTEXT_SHRINK_FACTOR)


class _MemSession:
    """Persistent in-memory session (get/clear/add) for compaction recovery."""

    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)

    async def get_items(self) -> list[Any]:
        return list(self._items)

    async def clear_session(self) -> None:
        self._items = []

    async def add_items(self, items: list[Any]) -> None:
        self._items.extend(items)


@pytest.mark.asyncio
async def test_reactive_compaction_recovers_and_bounds_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A size rejection with compaction enabled: the agent compacts its stored
    # session in place and recovers, without needing to shrink the budget. The
    # persisted session is smaller afterwards (bounded growth).
    from strix.core import execution

    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)

    context_filter = ContextLimitFilter(
        configured_window=2_000,
        compaction_trigger_ratio=0.7,
        compaction_keep_recent=2,
        summarizer_model="",  # offline: index-only compaction, no model call
    )
    shrink_spy = Mock(wraps=context_filter.shrink)
    context_filter.shrink = shrink_spy  # type: ignore[method-assign]
    run_config = _FakeRunConfig(context_filter)

    big = [{"role": "assistant", "content": "x" * 4000} for _ in range(10)]
    session = _MemSession([{"role": "user", "content": "TASK"}, *big])
    before = len(await session.get_items())

    class _RejectedError(Exception):
        status_code = 400

    message = (
        "Error code: 400 - maximum context length is 2000 tokens. "
        "However, your prompt contains at least 6000 input tokens..."
    )

    calls = {"n": 0}

    def flaky(*_args: Any, **_kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _RejectedError(message)
        return _FakeStream()

    monkeypatch.setattr(execution.Runner, "run_streamed", flaky)

    result = await execution._run_cycle(
        object(),
        coordinator,
        "root",
        input_data="task",
        run_config=run_config,
        context={"parent_id": None},
        max_turns=5,
        session=session,
        interactive=True,
        event_sink=None,
        hooks=None,
    )

    assert calls["n"] == 2  # one rejection, compacted, then succeeded
    assert result is not None
    assert coordinator.statuses["root"] not in {"failed", "crashed"}
    shrink_spy.assert_not_called()  # compaction handled it, no budget shrink needed
    assert len(await session.get_items()) < before  # stored session bounded

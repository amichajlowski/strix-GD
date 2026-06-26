"""Regression tests for root completion guardrails."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agents.tool_context import ToolContext

from strix.agents.factory import select_tools
from strix.core.agents import AgentCoordinator
from strix.report.state import ReportState, set_global_report_state
from strix.tools.finish.tool import _completion_blockers, _qa_review_blocker, finish_scan
from strix.tools.qa_loop.tool import compute_review_metrics
from strix.tools.todo import tools as todo_tools


@pytest.fixture(autouse=True)
def _clear_todos(tmp_path: Path) -> None:
    todo_tools.hydrate_todos_from_disk(tmp_path)


def _ready_review(rs: ReportState, coordinator: AgentCoordinator | None) -> dict:
    metrics = compute_review_metrics(rs, coordinator)
    return {"review_id": "qa_1", "ready_to_finish": True, "review_metrics": metrics}


def _deep_state(tmp_path: Path) -> ReportState:
    rs = ReportState("run-finish")
    rs._run_dir = tmp_path
    rs.set_scan_config({"targets": [{"type": "web_application"}], "scan_mode": "deep"})
    set_global_report_state(rs)
    return rs


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


# --------------------------------------------------------------------------- #
# QA review finish gate
# --------------------------------------------------------------------------- #


async def test_deep_completion_blocks_without_qa_review(tmp_path: Path) -> None:
    _deep_state(tmp_path)
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    blocker = _qa_review_blocker(
        {"qa_loop_enabled": True, "coordinator": coordinator, "agent_id": "root"}
    )
    assert blocker["scan_completed"] is False
    assert blocker["required_tool"] == "review_before_finish"


async def test_deep_completion_blocks_not_ready_qa_review(tmp_path: Path) -> None:
    rs = _deep_state(tmp_path)
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    review = _ready_review(rs, coordinator)
    review["ready_to_finish"] = False
    review["priority_gaps"] = [{"gap_id": "auth_jwt:jwt_authentication", "priority": "high"}]
    rs.record_qa_review(review)

    blocker = _qa_review_blocker(
        {"qa_loop_enabled": True, "coordinator": coordinator, "agent_id": "root"}
    )
    assert blocker["scan_completed"] is False
    assert blocker["qa_review"]["priority_gaps"]


async def test_deep_completion_allows_ready_fresh_qa_review(tmp_path: Path) -> None:
    rs = _deep_state(tmp_path)
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    rs.record_qa_review(_ready_review(rs, coordinator))

    blocker = _qa_review_blocker(
        {"qa_loop_enabled": True, "coordinator": coordinator, "agent_id": "root"}
    )
    assert blocker is None


def test_standard_completion_does_not_require_qa_review(tmp_path: Path) -> None:
    _deep_state(tmp_path)
    assert _qa_review_blocker({"qa_loop_enabled": False}) is None


async def test_stale_qa_review_blocks_finish_after_new_vulnerability(tmp_path: Path) -> None:
    rs = _deep_state(tmp_path)
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    rs.record_qa_review(_ready_review(rs, coordinator))

    rs.vulnerability_reports.append({"id": "vuln-0001", "title": "x", "severity": "high"})
    blocker = _qa_review_blocker(
        {"qa_loop_enabled": True, "coordinator": coordinator, "agent_id": "root"}
    )
    assert blocker is not None
    assert "stale" in blocker["error"]


async def test_review_metrics_identical_between_tool_and_finish_gate(tmp_path: Path) -> None:
    rs = _deep_state(tmp_path)
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    assert compute_review_metrics(rs, coordinator) == compute_review_metrics(rs, coordinator)


async def test_existing_unresolved_agent_blockers_still_win(tmp_path: Path) -> None:
    rs = _deep_state(tmp_path)
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    await coordinator.register("child", "Enumeration", parent_id="root")
    await coordinator.record_error("child", RuntimeError("boom"))
    await coordinator.set_status("child", "failed")
    rs.record_qa_review(_ready_review(rs, coordinator))

    args = json.dumps({"executive_summary": "a", "methodology": "b",
                       "technical_analysis": "c", "recommendations": "d"})
    ctx = ToolContext(
        context={
            "coordinator": coordinator,
            "agent_id": "root",
            "parent_id": None,
            "qa_loop_enabled": True,
        },
        tool_name="finish_scan",
        tool_call_id="t1",
        tool_arguments=args,
    )
    out = json.loads(await finish_scan.on_invoke_tool(ctx, args))
    assert out["scan_completed"] is False
    assert out["unresolved_agents"][0]["agent_id"] == "child"


# --------------------------------------------------------------------------- #
# Factory tool selection
# --------------------------------------------------------------------------- #


def test_root_agent_includes_review_before_finish_tool() -> None:
    names = {t.name for t in select_tools(is_root=True)}
    assert "review_before_finish" in names
    assert "finish_scan" in names


def test_child_agent_does_not_include_review_before_finish_tool() -> None:
    names = {t.name for t in select_tools(is_root=False)}
    assert "review_before_finish" not in names


def test_child_finish_is_not_qa_gated() -> None:
    names = {t.name for t in select_tools(is_root=False)}
    assert "agent_finish" in names
    assert "finish_scan" not in names


# --------------------------------------------------------------------------- #
# Prompt guidance
# --------------------------------------------------------------------------- #


def test_root_agent_skill_mentions_review_before_finish() -> None:
    text = Path("strix/skills/coordination/root_agent.md").read_text(encoding="utf-8")
    assert "review_before_finish" in text
    assert "finish_scan" in text

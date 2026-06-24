"""Tests for the TUI failed-agent recovery helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from strix.core.agents import AgentCoordinator
from strix.core.paths import runtime_state_dir
from strix.interface.tui import recovery
from strix.interface.tui.live_view import TuiLiveView
from strix.report.state import ReportState
from strix.report.writer import read_run_record


if TYPE_CHECKING:
    from pathlib import Path


class _FakeSession:
    def __init__(self) -> None:
        self.items: list[Any] = []

    async def add_items(self, items: list[Any]) -> None:
        self.items.extend(items)

    async def get_items(self) -> list[Any]:
        return list(self.items)


def test_graph_sync_hydrates_error_metadata() -> None:
    live_view = TuiLiveView()
    live_view.upsert_agent(
        "a1",
        name="strix",
        status="failed",
        last_error={"type": "RuntimeError", "message": "boom upstream"},
    )

    agent = live_view.agents["a1"]
    assert agent["last_error"]["type"] == "RuntimeError"
    assert agent["error_message"] == "boom upstream"


def test_failed_status_renders_recovery_prompt() -> None:
    agent_data = {
        "status": "failed",
        "last_error": {
            "type": "APIError",
            "message": "rate limited",
            "suggested_fix": "wait and retry",
        },
    }
    text = recovery.render_recovery_status(agent_data).plain
    assert "APIError" in text
    assert "rate limited" in text
    assert "wait and retry" in text


def test_tui_displays_checkpoint_warning() -> None:
    live_view = TuiLiveView()
    live_view.upsert_agent("a1", name="strix", status="running")
    live_view.upsert_agent("a1", checkpoint_warning="snapshot may be stale")

    agent = live_view.agents["a1"]
    assert agent["checkpoint_warning"] == "snapshot may be stale"
    # The warning is additive — it must not replace the agent's status.
    assert agent["status"] == "running"


def test_is_error_state_distinguishes_user_stop_from_error_stop() -> None:
    assert recovery.is_error_state({"status": "failed"})
    assert recovery.is_error_state({"status": "crashed"})
    assert recovery.is_error_state({"status": "stopped", "last_error": {"type": "X"}})
    assert not recovery.is_error_state({"status": "stopped"})
    assert not recovery.is_error_state({"status": "running"})


async def test_retry_failed_agent_reruns_and_clears_last_error() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("a1", "strix", parent_id=None)
    await coordinator.record_error("a1", RuntimeError("boom"))
    await coordinator.set_status("a1", "failed")
    coordinator.runtimes["a1"].session = _FakeSession()

    last_error = coordinator.metadata["a1"]["last_error"]
    message = recovery.build_retry_message(last_error)
    assert "RuntimeError" in message

    delivered = await coordinator.send(
        "a1", {"from": "user", "type": "instruction", "content": message}
    )
    assert delivered
    assert coordinator.pending_counts["a1"] == 1

    # Agent returns to running on its next cycle -> last_error clears.
    await coordinator.mark_running("a1")
    assert "last_error" not in coordinator.metadata["a1"]


def test_save_for_resume_preserves_agent_state(tmp_path: Path) -> None:
    report_state = ReportState("run-x")
    report_state._run_dir = tmp_path
    state_dir = runtime_state_dir(tmp_path)
    state_dir.mkdir(parents=True)
    (state_dir / "agents.json").write_text("{}", encoding="utf-8")
    (state_dir / "agents.db").write_text("db", encoding="utf-8")
    report_state.vulnerability_reports = [
        {
            "id": "vuln-0001",
            "title": "X",
            "severity": "high",
            "timestamp": "2026-01-01 00:00:00 UTC",
        }
    ]

    recovery.save_for_resume(report_state)

    assert (tmp_path / "run.json").exists()
    assert read_run_record(tmp_path)["status"] == "paused"
    assert (state_dir / "agents.json").exists()
    assert (state_dir / "agents.db").exists()
    assert (tmp_path / "vulnerabilities.json").exists()


async def test_cancel_keep_findings_discards_state_keeps_findings(tmp_path: Path) -> None:
    report_state = ReportState("run-x")
    report_state._run_dir = tmp_path
    report_state.vulnerability_reports = [
        {
            "id": "vuln-0001",
            "title": "X",
            "severity": "high",
            "timestamp": "2026-01-01 00:00:00 UTC",
        }
    ]

    state_dir = runtime_state_dir(tmp_path)
    state_dir.mkdir(parents=True)
    (state_dir / "agents.json").write_text("{}", encoding="utf-8")
    (state_dir / "agents.db").write_text("db", encoding="utf-8")

    coordinator = AgentCoordinator()
    coordinator.set_snapshot_path(state_dir / "agents.json")

    recovery.cancel_keep_findings(report_state, coordinator, tmp_path)

    assert report_state.run_record["status"] == "cancelled_findings_saved"
    assert (tmp_path / "vulnerabilities.json").exists()
    assert not (state_dir / "agents.json").exists()
    assert not (state_dir / "agents.db").exists()

    # A later generic cleanup must not downgrade the deliberate cancel status.
    report_state.cleanup(status="stopped")
    assert read_run_record(tmp_path)["status"] == "cancelled_findings_saved"

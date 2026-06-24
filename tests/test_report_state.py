"""Tests for run-status protection in ReportState."""

from __future__ import annotations

from typing import TYPE_CHECKING

from strix.report.state import ReportState
from strix.report.writer import read_run_record


if TYPE_CHECKING:
    from pathlib import Path


def test_paused_status_survives_stopped_cleanup(tmp_path: Path) -> None:
    report_state = ReportState("run-x")
    report_state._run_dir = tmp_path

    report_state.save_run_data(status="paused")
    assert report_state.run_record["status"] == "paused"

    report_state.cleanup(status="stopped")

    assert report_state.run_record["status"] == "paused"
    assert read_run_record(tmp_path)["status"] == "paused"


def test_cancelled_findings_saved_survives_interrupted_cleanup(tmp_path: Path) -> None:
    report_state = ReportState("run-x")
    report_state._run_dir = tmp_path

    report_state.save_run_data(status="cancelled_findings_saved")
    report_state.cleanup(status="interrupted")

    assert report_state.run_record["status"] == "cancelled_findings_saved"


def test_completed_status_is_not_overwritten(tmp_path: Path) -> None:
    report_state = ReportState("run-x")
    report_state._run_dir = tmp_path

    report_state.save_run_data(mark_complete=True)
    report_state.cleanup(status="stopped")

    assert report_state.run_record["status"] == "completed"

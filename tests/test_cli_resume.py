"""Tests for the --resume CLI gate (same-run restart vs hard failure)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from strix.core.paths import run_dir_for
from strix.interface.main import parse_arguments
from strix.report.writer import write_run_record


if TYPE_CHECKING:
    from pathlib import Path


def _write_run(run_name: str, *, status: str) -> None:
    run_dir = run_dir_for(run_name)
    run_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "run_id": run_name,
        "run_name": run_name,
        "status": status,
        "targets_info": [{"type": "web_application", "details": {"target_url": "https://x.test"}}],
        "scan_mode": "deep",
    }
    write_run_record(run_dir, record)


def test_resume_missing_agents_json_with_valid_run_json_allows_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_run("run-a", status="running")  # no .state/agents.json
    monkeypatch.setattr("sys.argv", ["strix", "--resume", "run-a"])

    args = parse_arguments()

    assert args.targets_info
    assert args.same_run_restart is True


def test_resume_cancelled_findings_saved_refuses_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_run("run-cancel", status="cancelled_findings_saved")
    monkeypatch.setattr("sys.argv", ["strix", "--resume", "run-cancel"])

    with pytest.raises(SystemExit):
        parse_arguments()


def test_resume_missing_run_json_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["strix", "--resume", "no-such-run"])

    with pytest.raises(SystemExit):
        parse_arguments()

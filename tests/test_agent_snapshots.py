"""Tests for snapshot retention, fallback, and write-failure warnings."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from strix.core.agents import AgentCoordinator
from strix.core.paths import run_dir_for, runtime_state_dir
from strix.core.runner import SAME_RUN_RESTART_INSTRUCTION, run_strix_scan
from strix.core.snapshots import (
    SnapshotError,
    load_latest_snapshot,
    previous_snapshot_path,
)


if TYPE_CHECKING:
    from pathlib import Path


_WEB_CONFIG: dict[str, Any] = {
    "targets": [{"type": "web_application", "details": {"target_url": "https://x.test"}}],
    "scan_mode": "deep",
}


async def test_snapshot_keeps_previous_good_copy(tmp_path: Path) -> None:
    coordinator = AgentCoordinator()
    path = tmp_path / "agents.json"
    coordinator.set_snapshot_path(path)

    await coordinator.register("root", "strix", parent_id=None)
    first = path.read_text(encoding="utf-8")

    await coordinator.register("child", "recon", parent_id="root")

    prev = previous_snapshot_path(path)
    assert prev.exists()
    assert prev.read_text(encoding="utf-8") == first


def test_resume_uses_latest_valid_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    prev = previous_snapshot_path(path)
    path.write_text(json.dumps({"statuses": {"a": "running"}}), encoding="utf-8")
    prev.write_text(json.dumps({"statuses": {"old": "running"}}), encoding="utf-8")

    snap, warning = load_latest_snapshot(path)

    assert snap == {"statuses": {"a": "running"}}
    assert warning is None


def test_resume_falls_back_to_previous_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    prev = previous_snapshot_path(path)
    path.write_text("{ not valid json", encoding="utf-8")
    prev.write_text(json.dumps({"statuses": {"old": "running"}}), encoding="utf-8")

    snap, warning = load_latest_snapshot(path)

    assert snap == {"statuses": {"old": "running"}}
    assert warning is not None
    assert "previous" in warning.lower()


def test_resume_fails_when_all_snapshots_invalid(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    prev = previous_snapshot_path(path)
    path.write_text("{ bad", encoding="utf-8")
    prev.write_text("{ also bad", encoding="utf-8")

    with pytest.raises(SnapshotError) as excinfo:
        load_latest_snapshot(path)

    message = str(excinfo.value)
    assert str(path) in message
    assert str(prev) in message


async def test_previous_snapshot_db_mismatch_routes_to_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    state_dir = runtime_state_dir(run_dir_for("run-fallback"))
    state_dir.mkdir(parents=True)
    # Current snapshot corrupt, previous snapshot valid (stale topology), db present.
    (state_dir / "agents.json").write_text("{ corrupt", encoding="utf-8")
    previous = {
        "statuses": {"root1234": "running", "childabc": "waiting"},
        "parent_of": {"root1234": None, "childabc": "root1234"},
        "names": {"root1234": "strix", "childabc": "recon"},
        "metadata": {"root1234": {"task": "t", "skills": []}},
        "pending_counts": {},
    }
    previous_snapshot_path(state_dir / "agents.json").write_text(
        json.dumps(previous), encoding="utf-8"
    )
    import sqlite3

    sqlite3.connect(str(state_dir / "agents.db")).close()  # valid empty SDK db

    captured: dict[str, Any] = {}
    _mock_sandbox_and_loop(monkeypatch, captured)

    await run_strix_scan(
        scan_config={**_WEB_CONFIG, "resume": True},
        scan_id="run-fallback",
        image="img",
        model="openai/gpt-test",
    )

    # Fell back to previous snapshot -> root-only restart, no deep child replay.
    assert SAME_RUN_RESTART_INSTRUCTION in captured["initial_input"]
    assert captured.get("respawn_called") is not True
    assert captured["agent_id"] == "root1234"


async def test_snapshot_write_failure_sets_checkpoint_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = AgentCoordinator()
    coordinator.set_snapshot_path(tmp_path / "agents.json")
    await coordinator.register("root", "strix", parent_id=None)

    import strix.core.agents as agents_mod

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(agents_mod.tempfile, "NamedTemporaryFile", boom)

    # Must not raise — a degraded checkpoint can't crash a live audit.
    await coordinator._maybe_snapshot()

    assert coordinator.checkpoint_warning is not None
    assert "failed" in coordinator.checkpoint_warning.lower()


def _mock_sandbox_and_loop(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    from strix.core import runner as runner_mod
    from strix.runtime import session_manager

    bundle = {"client": object(), "session": object(), "caido_client": None}

    async def fake_create(*_a: Any, **_k: Any) -> dict[str, Any]:
        return bundle

    async def fake_cleanup(*_a: Any, **_k: Any) -> None:
        return None

    async def fake_loop(**kwargs: Any) -> None:
        captured["agent_id"] = kwargs.get("agent_id")
        captured["initial_input"] = kwargs.get("initial_input")
        return None

    async def fake_respawn(**_k: Any) -> None:
        captured["respawn_called"] = True

    monkeypatch.setattr(session_manager, "create_or_reuse", fake_create)
    monkeypatch.setattr(session_manager, "cleanup", fake_cleanup)
    monkeypatch.setattr(runner_mod, "build_strix_agent", lambda **_k: object())
    monkeypatch.setattr(runner_mod, "make_child_factory", lambda **_k: (lambda **_kk: object()))
    monkeypatch.setattr(runner_mod, "run_agent_loop", fake_loop)
    monkeypatch.setattr(runner_mod, "respawn_subagents", fake_respawn)

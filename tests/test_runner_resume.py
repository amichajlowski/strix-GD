"""Tests for early checkpointing and same-run restart in run_strix_scan."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from strix.core.paths import run_dir_for, runtime_state_dir
from strix.core.runner import SAME_RUN_RESTART_INSTRUCTION, run_strix_scan


if TYPE_CHECKING:
    from pathlib import Path


_WEB_CONFIG: dict[str, Any] = {
    "targets": [{"type": "web_application", "details": {"target_url": "https://x.test"}}],
    "scan_mode": "deep",
}


def _agents_json(state_dir: Path) -> dict[str, Any]:
    return json.loads((state_dir / "agents.json").read_text(encoding="utf-8"))


def _mock_sandbox_and_loop(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> Any:
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
        captured["start_parked"] = kwargs.get("start_parked")
        return None

    async def fake_respawn(**_k: Any) -> None:
        captured["respawn_called"] = True

    monkeypatch.setattr(session_manager, "create_or_reuse", fake_create)
    monkeypatch.setattr(session_manager, "cleanup", fake_cleanup)
    monkeypatch.setattr(runner_mod, "build_strix_agent", lambda **_k: object())
    monkeypatch.setattr(runner_mod, "make_child_factory", lambda **_k: (lambda **_kk: object()))
    monkeypatch.setattr(runner_mod, "run_agent_loop", fake_loop)
    monkeypatch.setattr(runner_mod, "respawn_subagents", fake_respawn)
    return runner_mod


async def test_fresh_scan_writes_root_snapshot_before_sandbox_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    from strix.runtime import session_manager

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("sandbox failed to start")

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(session_manager, "create_or_reuse", boom)
    monkeypatch.setattr(session_manager, "cleanup", noop)

    with pytest.raises(RuntimeError, match="sandbox failed"):
        await run_strix_scan(
            scan_config=_WEB_CONFIG, scan_id="run-fresh", image="img", model="openai/gpt-test"
        )

    state_dir = runtime_state_dir(run_dir_for("run-fresh"))
    snap = _agents_json(state_dir)
    roots = [aid for aid, parent in snap["parent_of"].items() if parent is None]
    assert len(roots) == 1


async def test_startup_failure_records_root_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    from strix.runtime import session_manager

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("caido bootstrap timed out")

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(session_manager, "create_or_reuse", boom)
    monkeypatch.setattr(session_manager, "cleanup", noop)

    with pytest.raises(RuntimeError):
        await run_strix_scan(
            scan_config=_WEB_CONFIG, scan_id="run-err", image="img", model="openai/gpt-test"
        )

    snap = _agents_json(runtime_state_dir(run_dir_for("run-err")))
    root_id = next(aid for aid, parent in snap["parent_of"].items() if parent is None)
    last_error = snap["metadata"][root_id]["last_error"]
    assert last_error["type"] == "RuntimeError"
    assert "caido bootstrap" in last_error["message"]


async def test_fresh_scan_registers_root_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    captured: dict[str, Any] = {}
    _mock_sandbox_and_loop(monkeypatch, captured)

    await run_strix_scan(
        scan_config=_WEB_CONFIG, scan_id="run-once", image="img", model="openai/gpt-test"
    )

    snap = _agents_json(runtime_state_dir(run_dir_for("run-once")))
    roots = [aid for aid, parent in snap["parent_of"].items() if parent is None]
    assert len(roots) == 1
    assert len(snap["statuses"]) == 1
    # Fresh scans drive the root from the root task, not a recovery instruction.
    assert captured["initial_input"] == snap["metadata"][roots[0]]["task"]


async def test_same_run_restart_preserves_existing_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    run_dir = run_dir_for("run-restart")
    run_dir.mkdir(parents=True)
    vulns = [{"id": "vuln-0001", "title": "Prior", "severity": "high"}]
    (run_dir / "vulnerabilities.json").write_text(json.dumps(vulns), encoding="utf-8")

    captured: dict[str, Any] = {}
    _mock_sandbox_and_loop(monkeypatch, captured)

    # resume requested but no agents.json -> same-run restart.
    config = {**_WEB_CONFIG, "resume": True}
    await run_strix_scan(
        scan_config=config, scan_id="run-restart", image="img", model="openai/gpt-test"
    )

    assert SAME_RUN_RESTART_INSTRUCTION in captured["initial_input"]
    assert captured.get("respawn_called") is not True
    # Findings file is untouched by the runner.
    assert json.loads((run_dir / "vulnerabilities.json").read_text()) == vulns


async def test_same_run_restart_creates_root_only_session_when_db_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    state_dir = runtime_state_dir(run_dir_for("run-dbmiss"))
    state_dir.mkdir(parents=True)
    snapshot = {
        "statuses": {"root1234": "running", "childabc": "waiting"},
        "parent_of": {"root1234": None, "childabc": "root1234"},
        "names": {"root1234": "strix", "childabc": "recon"},
        "metadata": {"root1234": {"task": "t", "skills": []}, "childabc": {"task": "c"}},
        "pending_counts": {},
    }
    (state_dir / "agents.json").write_text(json.dumps(snapshot), encoding="utf-8")
    # No agents.db on disk.

    captured: dict[str, Any] = {}
    _mock_sandbox_and_loop(monkeypatch, captured)

    config = {**_WEB_CONFIG, "resume": True}
    await run_strix_scan(
        scan_config=config, scan_id="run-dbmiss", image="img", model="openai/gpt-test"
    )

    assert captured["agent_id"] == "root1234"  # restarts the existing root only
    assert SAME_RUN_RESTART_INSTRUCTION in captured["initial_input"]
    assert captured.get("respawn_called") is not True  # children not respawned
    assert (state_dir / "agents.db").exists()  # a fresh root session was opened

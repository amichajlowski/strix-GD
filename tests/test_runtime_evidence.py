"""Tests for the run evidence manifest and cleanup resilience."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from strix.core.paths import run_dir_for
from strix.core.runner import run_strix_scan
from strix.report.evidence import write_evidence_manifest
from strix.report.writer import write_run_record


if TYPE_CHECKING:
    from pathlib import Path


_WEB_CONFIG: dict[str, Any] = {
    "targets": [{"type": "web_application", "details": {"target_url": "https://x.test"}}],
    "scan_mode": "deep",
}


def _mock_sandbox_and_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    from strix.core import runner as runner_mod
    from strix.runtime import session_manager

    bundle = {"client": object(), "session": object(), "caido_client": None}

    async def fake_create(*_a: Any, **_k: Any) -> dict[str, Any]:
        return bundle

    async def fake_cleanup(*_a: Any, **_k: Any) -> None:
        return None

    async def fake_loop(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(session_manager, "create_or_reuse", fake_create)
    monkeypatch.setattr(session_manager, "cleanup", fake_cleanup)
    monkeypatch.setattr(runner_mod, "build_strix_agent", lambda **_k: object())
    monkeypatch.setattr(runner_mod, "make_child_factory", lambda **_k: (lambda **_kk: object()))
    monkeypatch.setattr(runner_mod, "run_agent_loop", fake_loop)


async def test_runner_finally_writes_evidence_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _mock_sandbox_and_loop(monkeypatch)
    local_sources = [{"source_path": "/workspace/repo", "workspace_subdir": "repo", "mount": False}]

    await run_strix_scan(
        scan_config=_WEB_CONFIG,
        scan_id="run-ev",
        image="img",
        model="openai/gpt-test",
        local_sources=local_sources,
    )

    manifest_path = run_dir_for("run-ev") / "evidence_manifest.json"
    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["workspace_sources"][0]["workspace_subdir"] == "repo"
    assert data["workspace_sources"][0]["source_path"] == "/workspace/repo"


def test_evidence_manifest_uses_shared_secret_scrubber(tmp_path: Path) -> None:
    write_evidence_manifest(
        run_dir=tmp_path,
        local_sources=[
            {"source_path": "/workspace/repo/app.py", "workspace_subdir": "repo", "mount": False}
        ],
        caido_url="http://user:topsecretpw@host:8080/p/1",
    )

    data = json.loads((tmp_path / "evidence_manifest.json").read_text(encoding="utf-8"))
    # Credential in the URL is scrubbed by the shared helper...
    assert "topsecretpw" not in json.dumps(data)
    assert data["caido_url"].startswith("http://XXXX@host")
    # ...while source paths and workspace mappings stay readable.
    assert data["workspace_sources"][0]["source_path"] == "/workspace/repo/app.py"
    assert data["workspace_sources"][0]["workspace_subdir"] == "repo"


async def test_cleanup_failure_does_not_block_findings_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    run_dir = run_dir_for("run-cf")
    run_dir.mkdir(parents=True)
    (run_dir / "vulnerabilities.json").write_text(
        json.dumps([{"id": "vuln-0001", "title": "X", "severity": "high"}]), encoding="utf-8"
    )
    write_run_record(
        run_dir,
        {"run_id": "run-cf", "run_name": "run-cf", "status": "running", "targets_info": [{}]},
    )

    _mock_sandbox_and_loop(monkeypatch)
    from strix.runtime import session_manager

    async def boom_cleanup(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("docker container delete failed")

    monkeypatch.setattr(session_manager, "cleanup", boom_cleanup)

    # A cleanup failure must not propagate or wipe persisted findings.
    await run_strix_scan(
        scan_config=_WEB_CONFIG, scan_id="run-cf", image="img", model="openai/gpt-test"
    )

    assert (run_dir / "vulnerabilities.json").exists()
    assert (run_dir / "run.json").exists()
    assert (run_dir / "evidence_manifest.json").exists()

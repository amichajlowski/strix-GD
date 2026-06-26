"""Tests for the QA loop: rules, tool-history summary, persistence, and review."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from agents.tool_context import ToolContext

from strix.core.agents import AgentCoordinator
from strix.core.tool_history import summarise_agent_tool_history
from strix.report.state import ReportState, set_global_report_state
from strix.tools.finish.tool import _qa_review_blocker
from strix.tools.notes import tools as note_tools
from strix.tools.qa_loop import tool as qa_tool
from strix.tools.qa_loop.rules import assemble_review, evaluate_qa_gaps, make_gap
from strix.tools.qa_loop.tool import (
    compute_review_metrics,
    metrics_match,
    review_before_finish,
)
from strix.tools.todo import tools as todo_tools


if TYPE_CHECKING:
    from pathlib import Path


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #


class _FakeSession:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    async def get_items(self) -> list[Any]:
        return list(self._items)


def _fc(name: str, args: dict[str, Any], call_id: str = "c1") -> dict[str, Any]:
    return {
        "type": "function_call",
        "name": name,
        "arguments": json.dumps(args),
        "call_id": call_id,
    }


def _ctx(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "target_types": set(),
        "tool_history": [],
        "tool_history_available": True,
        "tool_history_partial": False,
        "proxy_sitemap_available": False,
        "signal_text": [],
    }
    base.update(over)
    return base


def _th(tool_name: str = "exec_command", command: str | None = None,
        options: list[str] | None = None) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "command": command,
        "key_options": options or [],
        "status": "completed",
    }


def _ids(gaps: list[dict[str, Any]]) -> set[str]:
    return {g["gap_id"] for g in gaps}


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path) -> None:
    note_tools.hydrate_notes_from_disk(tmp_path)
    todo_tools.hydrate_todos_from_disk(tmp_path)


def _setup_report_state(tmp_path: Path, targets: list[dict[str, Any]],
                        scan_mode: str = "deep") -> ReportState:
    rs = ReportState("run-qa")
    rs._run_dir = tmp_path
    rs.set_scan_config({"targets": targets, "scan_mode": scan_mode})
    set_global_report_state(rs)
    return rs


# --------------------------------------------------------------------------- #
# Tool registration / subagent guard
# --------------------------------------------------------------------------- #


def _tc(inner: dict[str, Any], args: str = "{}") -> ToolContext:
    return ToolContext(
        context=inner, tool_name="review_before_finish", tool_call_id="t1", tool_arguments=args
    )


async def test_review_before_finish_rejects_subagent() -> None:
    ctx = _tc({"parent_id": "root", "agent_id": "child"})
    out = json.loads(await review_before_finish.on_invoke_tool(ctx, "{}"))
    assert out["success"] is False


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def test_review_persists_latest_review_to_run_record(tmp_path: Path) -> None:
    rs = ReportState("run-x")
    rs._run_dir = tmp_path
    rs.record_qa_review({"review_id": "qa_1", "ready_to_finish": True})
    assert rs.run_record["qa_review"]["review_id"] == "qa_1"

    reloaded = ReportState("run-x")
    reloaded._run_dir = tmp_path
    reloaded.hydrate_from_run_dir()
    assert reloaded.get_latest_qa_review()["review_id"] == "qa_1"


# --------------------------------------------------------------------------- #
# Tool history summary
# --------------------------------------------------------------------------- #


async def test_tool_history_extracts_exec_command_name_and_options() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    await coordinator.attach_runtime(
        "root",
        session=_FakeSession([_fc("exec_command", {"cmd": "nmap -sV -sC 10.0.0.1"})]),
    )
    summary = await summarise_agent_tool_history(coordinator)
    entry = summary["tool_history"][0]
    assert entry["command"] == "nmap"
    assert "-sV" in entry["key_options"]
    assert "-sC" in entry["key_options"]


async def test_tool_history_redacts_sensitive_command_values() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    await coordinator.attach_runtime(
        "root",
        session=_FakeSession([_fc("exec_command", {"cmd": "curl -H 'token=supersecret123' x"})]),
    )
    summary = await summarise_agent_tool_history(coordinator)
    blob = json.dumps(summary)
    # Flag values are dropped entirely (only basename + flag names kept), so the
    # secret never reaches the persisted summary.
    assert "supersecret123" not in blob
    assert summary["tool_history"][0]["command"] == "curl"


async def test_tool_history_bounds_per_agent_before_merge() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    old = [_fc("exec_command", {"cmd": "nmap"}, call_id=f"old{i}") for i in range(20)]
    newest = _fc("exec_command", {"cmd": "katana"}, call_id="new")
    await coordinator.attach_runtime("root", session=_FakeSession([*old, newest]))
    summary = await summarise_agent_tool_history(coordinator, per_agent_item_limit=1)
    assert len(summary["tool_history"]) == 1
    assert summary["tool_history"][0]["command"] == "katana"


async def test_tool_history_distinguishes_unavailable_from_empty() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    summary = await summarise_agent_tool_history(coordinator)
    assert summary["agents_with_sessions"] == 0
    assert summary["agents_total"] == 1


# --------------------------------------------------------------------------- #
# Recon rules
# --------------------------------------------------------------------------- #


def test_web_target_without_recon_gets_high_gap() -> None:
    gaps = evaluate_qa_gaps(
        _ctx(target_types={"web_application"}, tool_history=[_th(command="curl")])
    )
    gap = next(g for g in gaps if g["gap_id"] == "recon_web:web_path_discovery")
    assert gap["priority"] == "high"


def test_web_target_with_katana_or_ffuf_has_no_baseline_recon_gap() -> None:
    gaps = evaluate_qa_gaps(
        _ctx(target_types={"web_application"}, tool_history=[_th(command="katana")])
    )
    assert "recon_web:web_path_discovery" not in _ids(gaps)


def test_source_target_without_source_triage_gets_high_gap() -> None:
    gaps = evaluate_qa_gaps(
        _ctx(target_types={"local_code"}, tool_history=[_th(command="ls")])
    )
    gap = next(g for g in gaps if g["gap_id"] == "recon_source:source_triage")
    assert gap["priority"] == "high"


def test_source_target_with_semgrep_and_trivy_satisfies_source_triage() -> None:
    gaps = evaluate_qa_gaps(
        _ctx(
            target_types={"repository"},
            tool_history=[_th(command="semgrep"), _th(command="trivy", options=["fs"])],
        )
    )
    assert "recon_source:source_triage" not in _ids(gaps)


def test_target_type_mapping_repository_counts_as_source() -> None:
    gaps = evaluate_qa_gaps(_ctx(target_types={"repository"}, tool_history=[_th(command="ls")]))
    assert "recon_source:source_triage" in _ids(gaps)


# --------------------------------------------------------------------------- #
# Attack-vector rules
# --------------------------------------------------------------------------- #


def test_graphql_signal_without_graphql_testing_gets_gap() -> None:
    gaps = evaluate_qa_gaps(
        _ctx(
            target_types={"web_application"},
            tool_history=[_th(command="katana")],
            signal_text=["found /graphql endpoint"],
        )
    )
    gap = next(g for g in gaps if g["gap_id"] == "attack_graphql:graphql")
    assert gap["priority"] == "high"


def test_jwt_signal_without_jwt_testing_gets_gap() -> None:
    gaps = evaluate_qa_gaps(
        _ctx(
            target_types={"web_application"},
            tool_history=[_th(command="katana")],
            signal_text=["uses bearer token auth"],
        )
    )
    assert "auth_jwt:jwt_authentication" in _ids(gaps)


def test_upload_signal_without_upload_testing_gets_gap() -> None:
    gaps = evaluate_qa_gaps(
        _ctx(
            target_types={"web_application"},
            tool_history=[_th(command="katana")],
            signal_text=["avatar upload form"],
        )
    )
    assert "attack_upload:file_upload" in _ids(gaps)


def test_admin_user_signal_without_access_control_testing_gets_gap() -> None:
    gaps = evaluate_qa_gaps(
        _ctx(
            target_types={"web_application"},
            tool_history=[_th(command="katana")],
            signal_text=["/admin and /users/123"],
        )
    )
    gap = next(g for g in gaps if g["gap_id"] == "access_control:access_control_idor")
    assert gap["priority"] == "high"


# --------------------------------------------------------------------------- #
# Tool option rules
# --------------------------------------------------------------------------- #


def test_nmap_without_service_detection_flags_option_gap_for_ip_target() -> None:
    gaps = evaluate_qa_gaps(
        _ctx(target_types={"ip_address"}, tool_history=[_th(command="nmap", options=["-p-"])])
    )
    gap = next(g for g in gaps if g["gap_id"] == "option_nmap:nmap_service_detection")
    assert gap["priority"] == "medium"


def test_nuclei_default_run_with_known_technology_flags_medium_gap() -> None:
    gaps = evaluate_qa_gaps(
        _ctx(
            target_types={"web_application"},
            tool_history=[_th(command="katana"), _th(command="nuclei")],
            signal_text=["nginx detected"],
        )
    )
    gap = next(g for g in gaps if g["gap_id"] == "option_nuclei:nuclei_templates")
    assert gap["priority"] == "medium"


# --------------------------------------------------------------------------- #
# Assembly: ready / cap / acknowledge / stable ids
# --------------------------------------------------------------------------- #


def test_ready_true_when_no_high_or_critical_gaps() -> None:
    gaps = evaluate_qa_gaps(
        _ctx(target_types={"web_application"}, tool_history=[_th(command="katana")])
    )
    assembled = assemble_review(gaps, acknowledged_gaps=[], max_priority_gaps=5)
    assert assembled["ready_to_finish"] is True
    assert assembled["priority_gaps"] == []


def test_priority_gaps_are_capped() -> None:
    gaps = evaluate_qa_gaps(
        _ctx(
            target_types={"web_application", "ip_address", "repository"},
            tool_history=[_th(command="curl")],
            signal_text=["/graphql", "bearer token", "avatar upload", "/admin /users"],
        )
    )
    assembled = assemble_review(gaps, acknowledged_gaps=[], max_priority_gaps=5)
    assert len(assembled["priority_gaps"]) == 5
    # Overflow blocking gaps are not silently dropped — the omitted count is surfaced.
    full = assemble_review(gaps, acknowledged_gaps=[], max_priority_gaps=100)
    assert assembled["priority_gaps_truncated"] == len(full["priority_gaps"]) - 5
    assert assembled["priority_gaps_truncated"] >= 1
    assert full["priority_gaps_truncated"] == 0


def test_summary_not_misleading_when_all_priority_gaps_truncated() -> None:
    # max_priority_gaps=0 empties priority_gaps, but ready_to_finish is computed
    # on the full blocking set, so the summary must not claim "ready to finish".
    gaps = evaluate_qa_gaps(
        _ctx(target_types={"web_application"}, tool_history=[_th(command="curl")])
    )
    assembled = assemble_review(gaps, acknowledged_gaps=[], max_priority_gaps=0)
    assert assembled["ready_to_finish"] is False
    assert assembled["priority_gaps"] == []
    assert assembled["priority_gaps_truncated"] >= 1
    summary = qa_tool._summary_text(assembled)
    assert "ready to finish" not in summary.lower()
    assert "high-priority gap" in summary.lower()


def test_acknowledged_high_gap_allows_ready_and_lands_in_residual() -> None:
    gaps = evaluate_qa_gaps(
        _ctx(
            target_types={"web_application"},
            tool_history=[_th(command="katana")],
            signal_text=["/graphql"],
        )
    )
    assembled = assemble_review(
        gaps, acknowledged_gaps=["attack_graphql:graphql"], max_priority_gaps=5
    )
    assert assembled["ready_to_finish"] is True
    residual_ids = {g["gap_id"] for g in assembled["deferred_or_residual"]}
    assert "attack_graphql:graphql" in residual_ids
    assert "attack_graphql:graphql" not in _ids(assembled["priority_gaps"])


def test_gap_id_is_stable_across_re_evaluation() -> None:
    ctx = _ctx(
        target_types={"web_application"},
        tool_history=[_th(command="curl")],
        signal_text=["/graphql", "bearer token"],
    )
    first = _ids(evaluate_qa_gaps(ctx))
    second = _ids(evaluate_qa_gaps(ctx))
    assert first == second
    assembled = assemble_review(
        evaluate_qa_gaps(ctx),
        acknowledged_gaps=["auth_jwt:jwt_authentication"],
        max_priority_gaps=5,
    )
    residual_ids = {g["gap_id"] for g in assembled["deferred_or_residual"]}
    assert "auth_jwt:jwt_authentication" in residual_ids


def test_make_gap_id_is_deterministic() -> None:
    g1 = make_gap(rule_key="r", area_key="a", priority="high", area="A", reason="x",
                  suggested_action="y")
    g2 = make_gap(rule_key="r", area_key="a", priority="low", area="A2", reason="z",
                  suggested_action="w")
    assert g1["gap_id"] == g2["gap_id"] == "r:a"


# --------------------------------------------------------------------------- #
# Tool history availability gating
# --------------------------------------------------------------------------- #


def test_tool_history_unavailable_does_not_fire_false_recon_gaps() -> None:
    gaps = evaluate_qa_gaps(
        _ctx(target_types={"web_application", "ip_address"}, tool_history_available=False)
    )
    assert "recon_web:web_path_discovery" not in _ids(gaps)
    assert "recon_ip:port_service_discovery" not in _ids(gaps)
    diag = [g for g in gaps if g["gap_id"] == "diagnostic_tool_history:unavailable"]
    assert len(diag) == 1
    assert diag[0]["priority"] == "low"


def test_partial_tool_history_failure_downgrades_absence_gaps() -> None:
    gaps = evaluate_qa_gaps(
        _ctx(
            target_types={"web_application"},
            tool_history=[_th(command="curl")],
            tool_history_partial=True,
        )
    )
    gap = next(g for g in gaps if g["gap_id"] == "recon_web:web_path_discovery")
    assert gap["priority"] == "medium"
    assert "diagnostic_tool_history:partial" in _ids(gaps)


# --------------------------------------------------------------------------- #
# Full review tool: privacy, persistence, cumulative acknowledgement
# --------------------------------------------------------------------------- #


async def _run_tool(coordinator: AgentCoordinator | None, **kwargs: Any) -> dict[str, Any]:
    inner: dict[str, Any] = {
        "agent_id": "root",
        "parent_id": None,
        "qa_loop_enabled": True,
        "coordinator": coordinator,
    }
    args = json.dumps(kwargs)
    return json.loads(await review_before_finish.on_invoke_tool(_tc(inner, args), args))


async def test_review_does_not_persist_note_or_query_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_report_state(tmp_path, [{"type": "web_application"}])
    note_tools._notes_storage["n1"] = {
        "title": "secret bearer eyJabc.def.ghi",
        "content": "password=hunter2supersecret",
        "category": "findings",
        "tags": ["token=leakedvalue123"],
        "created_at": "t",
        "updated_at": "t",
    }

    async def fake_proxy(_inner: dict[str, Any]) -> tuple[list[str], bool]:
        return ["/search?token=querysecret999"], True

    monkeypatch.setattr(qa_tool, "_collect_proxy", fake_proxy)

    out = await _run_tool(None)
    blob = json.dumps(out)
    assert "hunter2supersecret" not in blob
    assert "querysecret999" not in blob
    assert "leakedvalue123" not in blob
    assert "content_preview" not in blob
    refs = out["note_refs"]
    assert refs[0]["note_id"] == "n1"
    assert "title" not in refs[0]
    assert "XXXX" in json.dumps(refs)


async def test_returned_and_persisted_review_match(tmp_path: Path) -> None:
    rs = _setup_report_state(tmp_path, [{"type": "web_application"}])
    out = await _run_tool(None)
    assert rs.get_latest_qa_review() == out


async def test_acknowledged_gaps_persist_across_subsequent_reviews(tmp_path: Path) -> None:
    rs = _setup_report_state(tmp_path, [{"type": "web_application"}])
    note_tools._notes_storage["n1"] = {
        "title": "graphql", "content": "/graphql endpoint", "category": "findings",
        "tags": [], "created_at": "t", "updated_at": "t",
    }
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    await coordinator.attach_runtime(
        "root", session=_FakeSession([_fc("exec_command", {"cmd": "katana -u x"})])
    )

    first = await _run_tool(coordinator, acknowledged_gaps=["attack_graphql:graphql"])
    assert "attack_graphql:graphql" in first["acknowledged_gaps"]

    rs.vulnerability_reports.append(
        {"id": "vuln-0001", "title": "x", "severity": "low", "timestamp": "t"},
    )
    second = await _run_tool(coordinator)
    assert "attack_graphql:graphql" in second["acknowledged_gaps"]
    residual_ids = {g["gap_id"] for g in second["deferred_or_residual"]}
    assert "attack_graphql:graphql" in residual_ids


async def test_review_continues_when_proxy_unavailable(tmp_path: Path) -> None:
    _setup_report_state(tmp_path, [{"type": "web_application"}])
    out = await _run_tool(None)
    assert out["success"] is True
    assert any("proxy" in w.lower() for w in out["diagnostics"]["warnings"])


async def test_collect_proxy_extracts_paths_and_satisfies_web_recon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_sitemap(_client: Any) -> dict[str, Any]:
        return {
            "entries": [
                {"request": {"path": "/admin"}},
                {"request": {"path": "/users"}},
                {"label": "no-request"},  # missing request -> skipped
                {"request": {"method": "GET"}},  # missing path -> skipped
            ]
        }

    monkeypatch.setattr(qa_tool.caido_api, "list_sitemap_with_client", fake_sitemap)
    paths, ok = await qa_tool._collect_proxy({"caido_client": object()})
    assert ok is True
    assert paths == ["/admin", "/users"]

    # Available tool history with no web-recon tool: recon_web fires without proxy,
    # but proxy paths supply the missing path-discovery signal and suppress it.
    rs = _setup_report_state(tmp_path, [{"type": "web_application"}])
    avail = {
        "tool_history": [{"command": "curl"}],
        "agents_with_sessions": 1,
        "agents_total": 1,
        "extraction_errors": [],
    }
    no_proxy = qa_tool._build_review_context(rs, avail, [], proxy_ok=False)
    assert "recon_web:web_path_discovery" in _ids(evaluate_qa_gaps(no_proxy))

    with_proxy = qa_tool._build_review_context(rs, avail, paths, ok)
    assert with_proxy["proxy_sitemap_available"] is True
    assert "/admin" in with_proxy["signal_text"]
    assert "recon_web:web_path_discovery" not in _ids(evaluate_qa_gaps(with_proxy))


# --------------------------------------------------------------------------- #
# Shared metrics + resume/stale gating
# --------------------------------------------------------------------------- #


def test_compute_review_metrics_shape(tmp_path: Path) -> None:
    rs = _setup_report_state(tmp_path, [{"type": "web_application"}])
    metrics = compute_review_metrics(rs, None)
    assert metrics["vulnerability_count"] == 0
    assert metrics["agent_count"] == 0
    assert metrics["unresolved_todo_count"] == 0


async def test_resumed_ready_review_with_matching_metrics_allows_finish(tmp_path: Path) -> None:
    rs = _setup_report_state(tmp_path, [{"type": "web_application"}])
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    metrics = compute_review_metrics(rs, coordinator)
    rs.record_qa_review({"review_id": "qa_1", "ready_to_finish": True, "review_metrics": metrics})

    blocker = _qa_review_blocker(
        {"qa_loop_enabled": True, "coordinator": coordinator, "agent_id": "root"}
    )
    assert blocker is None


async def test_resumed_ready_review_with_changed_metrics_reblocks(tmp_path: Path) -> None:
    rs = _setup_report_state(tmp_path, [{"type": "web_application"}])
    coordinator = AgentCoordinator()
    await coordinator.register("root", "strix", parent_id=None)
    metrics = compute_review_metrics(rs, coordinator)
    rs.record_qa_review({"review_id": "qa_1", "ready_to_finish": True, "review_metrics": metrics})

    rs.vulnerability_reports.append(
        {"id": "vuln-0001", "title": "x", "severity": "low", "timestamp": "t"},
    )
    blocker = _qa_review_blocker(
        {"qa_loop_enabled": True, "coordinator": coordinator, "agent_id": "root"}
    )
    assert blocker is not None
    assert "stale" in blocker["error"]


def test_metrics_match_helper() -> None:
    base = {"vulnerability_count": 1, "agent_count": 2, "unresolved_todo_count": 0}
    assert metrics_match(base, dict(base)) is True
    assert metrics_match(base, {**base, "agent_count": 3}) is False
    assert metrics_match(None, base) is False

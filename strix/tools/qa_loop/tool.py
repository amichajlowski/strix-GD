"""``review_before_finish`` — root-only pre-finish QA review gate.

Collects a bounded review context from existing run artefacts, evaluates
deterministic gap rules, and persists a compact result under
``run.json["qa_review"]``. No internal LLM call, no new persistence service.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agents import RunContextWrapper, function_tool

from strix.core.agents import coordinator_from_context
from strix.core.scrubbing import scrub_secrets
from strix.core.tool_history import summarise_agent_tool_history
from strix.report.state import get_global_report_state
from strix.tools.notes.tools import qa_notes_summary
from strix.tools.proxy import caido_api
from strix.tools.qa_loop.rules import assemble_review, evaluate_qa_gaps
from strix.tools.todo.tools import unresolved_todos_summary


if TYPE_CHECKING:
    from strix.core.agents import AgentCoordinator
    from strix.report.state import ReportState


logger = logging.getLogger(__name__)

_MAX_FIELD_LEN = 500
_MAX_PROXY_PATHS = 50


def compute_review_metrics(
    report_state: ReportState,
    coordinator: AgentCoordinator | None,
) -> dict[str, Any]:
    """Cheap metrics shared by the review tool and the finish gate.

    Intentionally avoids tool-history extraction so the finish gate can call it
    cheaply. Stale detection compares vulnerability/agent/unresolved-todo counts.
    """
    scan_config = report_state.scan_config or {}
    scan_mode = str(scan_config.get("scan_mode") or report_state.run_record.get("scan_mode") or "")
    agent_count = len(coordinator.statuses) if coordinator is not None else 0
    unresolved_todo_count = sum(s["total_unresolved"] for s in unresolved_todos_summary())
    return {
        "scan_mode": scan_mode,
        "vulnerability_count": len(report_state.vulnerability_reports),
        "agent_count": agent_count,
        "unresolved_todo_count": unresolved_todo_count,
    }


def metrics_match(persisted: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    """True if the cheap stale-check fields are unchanged."""
    if not isinstance(persisted, dict):
        return False
    keys = ("vulnerability_count", "agent_count", "unresolved_todo_count")
    return all(persisted.get(k) == current.get(k) for k in keys)


def _strip_query(path: str) -> str:
    return path.split("?", 1)[0].split("#", 1)[0]


async def _collect_proxy(inner: dict[str, Any]) -> tuple[list[str], bool]:
    """Best-effort proxy path samples (path only, query stripped).

    Returns ``(paths, ok)``. ``ok=False`` means the optional source was
    unavailable — not configured, or the sitemap fetch failed — and the caller
    records a diagnostic rather than crashing. ``ok=True`` with no paths means
    the proxy is reachable but captured nothing yet.

    ponytail: root sitemap level only (no recursive descent) — enough to signal
    that path discovery happened; deepen if coverage rules need more.
    """
    client = inner.get("caido_client")
    if client is None:
        return [], False
    try:
        result = await caido_api.list_sitemap_with_client(client)
    except Exception:  # noqa: BLE001 - degraded source must not crash the audit
        logger.debug("QA review: Caido sitemap fetch failed", exc_info=True)
        return [], False
    paths = [
        str(path)
        for entry in result.get("entries") or []
        if (path := (entry.get("request") or {}).get("path"))
    ]
    return paths, True


def _build_review_context(
    report_state: ReportState,
    tool_history: dict[str, Any],
    proxy_paths: list[str],
    proxy_ok: bool,
) -> dict[str, Any]:
    scan_config = report_state.scan_config or {}
    targets = scan_config.get("targets") or []
    target_types = {t.get("type") for t in targets if isinstance(t, dict)}

    n_sess = int(tool_history.get("agents_with_sessions") or 0)
    errs = tool_history.get("extraction_errors") or []
    hist = tool_history.get("tool_history") or []
    all_failed = n_sess > 0 and not hist and bool(errs)
    available = n_sess > 0 and not all_failed
    partial = available and bool(errs)

    notes = qa_notes_summary()
    signal_text: list[str] = list(notes["signals"])
    signal_text.extend(str(v.get("title", "")).lower() for v in report_state.vulnerability_reports)
    signal_text.extend(p.lower() for p in proxy_paths)

    return {
        "target_types": target_types,
        "tool_history": hist,
        "tool_history_available": available,
        "tool_history_partial": partial,
        "proxy_sitemap_available": proxy_ok and bool(proxy_paths),
        "signal_text": signal_text,
        "_note_refs": notes["refs"],
    }


def _scrub_text(text: Any) -> str:
    return scrub_secrets(str(text))[:_MAX_FIELD_LEN]


def _scrub_gap(gap: dict[str, Any]) -> dict[str, Any]:
    out = dict(gap)
    for key in ("area", "reason", "suggested_action"):
        if key in out:
            out[key] = _scrub_text(out[key])
    if "evidence" in out:
        out["evidence"] = [_scrub_text(e) for e in out.get("evidence") or []]
    return out


def _summary_text(assembled: dict[str, Any]) -> str:
    gaps = assembled["priority_gaps"]
    if not gaps:
        return "No high-priority gaps found; audit quality review is ready to finish."
    areas = ", ".join(str(g.get("area", "")) for g in gaps)
    return f"Review found {len(gaps)} high-priority gap(s): {areas}."


async def _run_review(
    inner: dict[str, Any],
    *,
    reason: str,
    max_priority_gaps: int,
    acknowledged_gaps: list[str] | None,
) -> dict[str, Any]:
    report_state = get_global_report_state()
    if report_state is None:
        return {"success": False, "error": "Report state unavailable; cannot run QA review"}

    coordinator = coordinator_from_context(inner)
    if coordinator is not None:
        tool_history = await summarise_agent_tool_history(coordinator)
    else:
        tool_history = {
            "tool_history": [],
            "agents_total": 0,
            "agents_with_sessions": 0,
            "extraction_errors": [],
        }

    proxy_paths, proxy_ok = await _collect_proxy(inner)
    proxy_paths = [_strip_query(p) for p in proxy_paths][:_MAX_PROXY_PATHS]

    review_context = _build_review_context(report_state, tool_history, proxy_paths, proxy_ok)
    gaps = evaluate_qa_gaps(review_context)

    prev = report_state.get_latest_qa_review() or {}
    prev_ack = prev.get("acknowledged_gaps") or []
    ack_union = sorted(set(prev_ack) | set(acknowledged_gaps or []))

    assembled = assemble_review(
        gaps, acknowledged_gaps=ack_union, max_priority_gaps=max_priority_gaps
    )
    metrics = compute_review_metrics(report_state, coordinator)

    now = datetime.now(UTC)
    diagnostics: dict[str, Any] = {
        "tool_history_available": review_context["tool_history_available"],
        "agents_with_sessions": tool_history["agents_with_sessions"],
        "agents_total": tool_history["agents_total"],
        "warnings": [],
    }
    if not proxy_ok:
        diagnostics["warnings"].append("Proxy sitemap unavailable; path-coverage signal skipped.")

    review = {
        "success": True,
        "ready_to_finish": assembled["ready_to_finish"],
        "review_id": f"qa_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:4]}",
        "created_at": now.isoformat(),
        "reason": _scrub_text(reason),
        "summary": _scrub_text(_summary_text(assembled)),
        "acknowledged_gaps": assembled["acknowledged_gaps"],
        "priority_gaps": [_scrub_gap(g) for g in assembled["priority_gaps"]],
        "priority_gaps_truncated": assembled["priority_gaps_truncated"],
        "deferred_or_residual": [_scrub_gap(g) for g in assembled["deferred_or_residual"]],
        "review_metrics": metrics,
        "diagnostics": diagnostics,
        "note_refs": review_context["_note_refs"],
    }
    report_state.record_qa_review(review)
    return review


@function_tool(timeout=120)
async def review_before_finish(
    ctx: RunContextWrapper,
    reason: str = "pre-finish audit quality review",
    max_priority_gaps: int = 5,
    acknowledged_gaps: list[str] | None = None,
) -> str:
    """Run a pre-finish audit-quality review (**root-agent only**).

    Inspects existing run evidence — recon/tool history, notes, findings,
    agent graph, todos — and reports high-value gaps that should be run or
    explicitly accepted before ``finish_scan``. For deep scans, ``finish_scan``
    is blocked until this returns ``ready_to_finish: true`` with fresh metrics.

    Workflow:

    1. Call this before ``finish_scan``.
    2. For each high/critical gap, spawn focused follow-up work (at most three
       workstreams per review) or validate it by other means.
    3. Re-run this after follow-up completes.
    4. If a high/critical gap is validated elsewhere, out of scope, or accepted
       as residual risk, re-run with its ``gap_id`` in ``acknowledged_gaps`` and
       document it in the final report. Acknowledgements are cumulative.

    Args:
        reason: Short note on why the review is being run.
        max_priority_gaps: Cap on returned high-priority gaps.
        acknowledged_gaps: ``gap_id`` values to accept as residual/out-of-scope;
            they move to ``deferred_or_residual`` and stop blocking completion.
    """
    inner = ctx.context if isinstance(ctx.context, dict) else {}
    if inner.get("parent_id") is not None:
        return json.dumps(
            {
                "success": False,
                "error": (
                    "This tool can only be used by the root/main agent. Subagents do not run "
                    "the pre-finish QA review."
                ),
            },
            ensure_ascii=False,
            default=str,
        )

    result = await _run_review(
        inner,
        reason=reason,
        max_priority_gaps=max_priority_gaps,
        acknowledged_gaps=acknowledged_gaps,
    )
    return json.dumps(result, ensure_ascii=False, default=str)

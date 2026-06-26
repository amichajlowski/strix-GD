"""``finish_scan`` — root-agent termination + executive report persistence."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agents import RunContextWrapper, function_tool

from strix.core.agents import coordinator_from_context
from strix.tools.todo.tools import unresolved_todos_summary


logger = logging.getLogger(__name__)


def _do_finish(
    *,
    parent_id: str | None,
    executive_summary: str,
    methodology: str,
    technical_analysis: str,
    recommendations: str,
) -> dict[str, Any]:
    if parent_id is not None:
        return {
            "success": False,
            "error": (
                "This tool can only be used by the root/main agent. "
                "If you are a subagent, use agent_finish instead"
            ),
        }

    errors: list[str] = []
    if not executive_summary.strip():
        errors.append("Executive summary cannot be empty")
    if not methodology.strip():
        errors.append("Methodology cannot be empty")
    if not technical_analysis.strip():
        errors.append("Technical analysis cannot be empty")
    if not recommendations.strip():
        errors.append("Recommendations cannot be empty")
    if errors:
        return {"success": False, "error": "Validation failed", "errors": errors}

    try:
        from strix.report.state import get_global_report_state

        report_state = get_global_report_state()
        if report_state is None:
            logger.warning("No global report state; scan results not persisted")
            return {
                "success": True,
                "scan_completed": True,
                "message": "Scan completed (not persisted)",
                "warning": "Results could not be persisted - report state unavailable",
            }
        report_state.update_scan_final_fields(
            executive_summary=executive_summary.strip(),
            methodology=methodology.strip(),
            technical_analysis=technical_analysis.strip(),
            recommendations=recommendations.strip(),
        )
        vuln_count = len(report_state.vulnerability_reports)
    except (ImportError, AttributeError) as e:
        logger.exception("finish_scan persistence failed")
        return {"success": False, "error": f"Failed to complete scan: {e!s}"}
    else:
        logger.info(
            "finish_scan: completed scan with %d vulnerability report(s)",
            vuln_count,
        )
        return {
            "success": True,
            "scan_completed": True,
            "message": "Scan completed successfully",
            "vulnerabilities_found": vuln_count,
        }


def _qa_review_blocker(inner: dict[str, Any]) -> dict[str, Any] | None:
    """Block deep-scan completion until a fresh, ready QA review exists.

    Uses only the cheap shared metrics helper — never recomputes tool history.
    Degraded persistence (no global report state) does not block on QA alone.
    """
    if not inner.get("qa_loop_enabled"):
        return None

    from strix.report.state import get_global_report_state

    report_state = get_global_report_state()
    if report_state is None:
        return None

    review = report_state.get_latest_qa_review()
    if review is None:
        return {
            "success": False,
            "scan_completed": False,
            "error": "QA review required before finishing a deep scan",
            "required_tool": "review_before_finish",
        }

    from strix.tools.qa_loop.tool import compute_review_metrics, metrics_match

    coordinator = coordinator_from_context(inner)
    current = compute_review_metrics(report_state, coordinator)
    if not metrics_match(review.get("review_metrics"), current):
        return {
            "success": False,
            "scan_completed": False,
            "error": "QA review is stale; run review_before_finish again",
            "required_tool": "review_before_finish",
        }

    if not review.get("ready_to_finish"):
        return {
            "success": False,
            "scan_completed": False,
            "error": "Cannot finish scan while QA review has high-priority gaps",
            "qa_review": {
                "review_id": review.get("review_id"),
                "priority_gaps": review.get("priority_gaps", []),
            },
        }
    return None


async def _completion_blockers(inner: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    coordinator = coordinator_from_context(inner)
    me = inner.get("agent_id")
    parent_id = inner.get("parent_id")
    if coordinator is None or parent_id is not None or not isinstance(me, str):
        return {"unresolved_agents": [], "unresolved_todos": []}

    unresolved_agents = await coordinator.unresolved_agents_except(me)
    todo_agent_ids = {me}
    todo_agent_ids.update(
        str(agent["agent_id"]) for agent in unresolved_agents if "agent_id" in agent
    )
    return {
        "unresolved_agents": unresolved_agents,
        "unresolved_todos": unresolved_todos_summary(todo_agent_ids),
    }


@function_tool(timeout=60)
async def finish_scan(
    ctx: RunContextWrapper,
    executive_summary: str,
    methodology: str,
    technical_analysis: str,
    recommendations: str,
) -> str:
    """Finalize the scan — persist the customer-facing report.

    **Root-agent only.** Subagents must call ``agent_finish`` from the
    multi-agent graph tools instead. Calling this finalizes everything:

    1. Verifies you are the root agent.
    2. Writes the four narrative sections to the scan record.
    3. Marks the scan completed and stops execution.

    **Pre-flight checklist (mandatory — do not skip):**

    1. **Call ``view_agent_graph`` first.** Inspect every entry in the
       summary. If ANY agent is ``running`` / ``waiting`` /
       ``failed`` / ``crashed`` — or ``stopped`` with a recorded error —
       you MUST NOT call ``finish_scan`` yet. Wrap active agents up via
       ``send_message_to_agent`` (ask them to finish),
       ``wait_for_message`` (block until their report arrives), or
       ``stop_agent`` (graceful cancel). Retry, reassign, or explicitly
       cancel failed/error agents for resume before finishing. Only
       ``completed`` agents and deliberate clean ``stopped`` agents are
       safe to leave behind.
    2. Your root todo list and unresolved failed/error agents' todo
       lists must have no pending or in-progress work. Mark completed
       work done, delete abandoned todos, or reassign the work before
       finalising.
    3. All vulnerabilities you found are filed via
       ``create_vulnerability_report`` (un-reported findings are not
       tracked and not credited).
    4. Don't double-report — one report per distinct vulnerability.

    **Calling this multiple times overwrites the previous report.**
    Make the single call comprehensive.

    **Customer-facing report rules** (this output is rendered into the
    final PDF the client sees):

    - Never mention internal infrastructure: no local/absolute paths
      (``/workspace/...``), no agent names, no sandbox/orchestrator/
      tooling references, no system prompts, no model-internal errors.
    - Tone: formal, third-person, objective, concise. This is a
      consultant deliverable, not an engineering log.
    - Each section has a specific role:

        - ``executive_summary`` — for non-technical leadership. Risk
          posture, business impact (data exposure / compliance /
          reputation), notable criticals, overarching remediation
          theme.
        - ``methodology`` — frameworks followed (OWASP WSTG, PTES,
          OSSTMM, NIST), engagement type (black/gray/white box), scope
          and constraints, categories of testing performed. **No**
          internal execution detail.
        - ``technical_analysis`` — consolidated findings overview with
          severity model and systemic root causes. Reference individual
          vuln reports for repro steps; don't duplicate raw evidence.
        - ``recommendations`` — prioritized actions grouped by urgency
          (Immediate / Short-term / Medium-term), each with concrete
          remediation steps. End with retest/validation guidance.

    Args:
        executive_summary: Business-level summary for leadership.
        methodology: Frameworks, scope, and approach.
        technical_analysis: Consolidated findings + systemic themes.
        recommendations: Prioritized, actionable remediation.
    """
    inner = ctx.context if isinstance(ctx.context, dict) else {}
    me = inner.get("agent_id")
    parent_id = inner.get("parent_id")
    coordinator = coordinator_from_context(inner)
    blockers = await _completion_blockers(inner)

    if blockers["unresolved_agents"] or blockers["unresolved_todos"]:
        return json.dumps(
            {
                "success": False,
                "scan_completed": False,
                "error": (
                    "Cannot finish scan while unresolved agent work remains. "
                    "Resolve failed/crashed/error-stopped agents and finish, delete, "
                    "or reassign open todos first"
                ),
                "unresolved_agents": blockers["unresolved_agents"],
                "unresolved_todos": blockers["unresolved_todos"],
            },
            ensure_ascii=False,
            default=str,
        )

    qa_blocker = _qa_review_blocker(inner)
    if qa_blocker is not None:
        return json.dumps(qa_blocker, ensure_ascii=False, default=str)

    result = await asyncio.to_thread(
        _do_finish,
        parent_id=parent_id,
        executive_summary=executive_summary,
        methodology=methodology,
        technical_analysis=technical_analysis,
        recommendations=recommendations,
    )
    if (
        result.get("success")
        and result.get("scan_completed")
        and coordinator is not None
        and isinstance(me, str)
    ):
        await coordinator.set_status(me, "completed")
    return json.dumps(result, ensure_ascii=False, default=str)

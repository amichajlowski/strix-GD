"""SDK run hooks used by Strix orchestration."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

from agents.lifecycle import RunHooks

from strix.core import reflection
from strix.report.state import get_global_report_state


if TYPE_CHECKING:
    from agents import RunContextWrapper
    from agents.agent import Agent
    from agents.items import ModelResponse


logger = logging.getLogger(__name__)


class BudgetExceededError(RuntimeError):
    """Raised when the accumulated LLM cost reaches the configured budget."""


class ReportUsageHooks(RunHooks[dict[str, Any]]):
    """Persist SDK-native usage after every model response."""

    def __init__(self, *, model: str, max_budget_usd: float | None = None) -> None:
        if max_budget_usd is not None and (
            not math.isfinite(max_budget_usd) or max_budget_usd <= 0
        ):
            raise ValueError("max_budget_usd must be a finite number greater than 0")
        self._model = model
        self._max_budget_usd = max_budget_usd

    async def on_llm_end(
        self,
        context: RunContextWrapper[dict[str, Any]],
        agent: Agent[dict[str, Any]],
        response: ModelResponse,
    ) -> None:
        report_state = get_global_report_state()
        if report_state is None:
            return

        ctx = context.context if isinstance(context.context, dict) else {}
        agent_name = getattr(agent, "name", None)
        if not isinstance(agent_name, str):
            agent_name = None
        agent_id = ctx.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            agent_id = agent_name or "unknown"

        try:
            report_state.record_sdk_usage(
                agent_id=agent_id,
                agent_name=agent_name,
                model=self._model,
                usage=response.usage,
            )
        except Exception:
            logger.exception("failed to record SDK usage for agent %s", agent_id)

        if self._max_budget_usd is not None:
            cost = report_state.get_total_llm_cost()
            if cost >= self._max_budget_usd:
                raise BudgetExceededError(
                    f"Token budget of ${self._max_budget_usd:.2f} exceeded (spent ${cost:.4f})"
                )

    async def on_agent_end(
        self,
        context: RunContextWrapper[dict[str, Any]],
        agent: Agent[dict[str, Any]],  # noqa: ARG002 — RunHooks signature
        output: Any,  # noqa: ARG002 — RunHooks signature
    ) -> None:
        """Fire the adaptive-audit reflection when a specialist CHILD finishes.

        Runs on the agent-teardown path, so it MUST never raise into the caller
        (R5) — the whole body is wrapped. Only child ends trigger it (a root end
        means the scan is over); budget-stopped / shutting-down runs skip. The
        reflection is scheduled as an exception-isolated background task so the
        completing child's teardown is never blocked.
        """
        try:
            ctx = context.context if isinstance(context.context, dict) else {}
            if ctx.get("agent_id") is None or ctx.get("parent_id") is None:
                return  # root end = scan over; don't reflect
            coordinator = ctx.get("coordinator")
            if coordinator is not None and (
                getattr(coordinator, "budget_stopped", False)
                or getattr(coordinator, "is_shutting_down", False)
            ):
                return  # respect budget stop / shutdown
            reflection._schedule_reflection(
                model=self._model,
                caido_client=ctx.get("caido_client"),
                coordinator=coordinator,
            )
        except Exception:  # a reflection bug must not crash an agent
            logger.exception("on_agent_end reflection scheduling failed")

"""Helpers for the TUI failed-agent recovery flow.

Pure logic kept out of the Textual ``App`` so it can be tested without a
running terminal: rendering recovery status text, building a safe retry
instruction, and the save/cancel persistence actions.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from rich.text import Text

from strix.core.paths import runtime_state_dir
from strix.report.state import CANCELLED_FINDINGS_SAVED_STATUS, PAUSED_STATUS


if TYPE_CHECKING:
    from pathlib import Path

    from strix.core.agents import AgentCoordinator
    from strix.report.state import ReportState


# Statuses that put an agent into a recoverable error state in the TUI.
RECOVERABLE_STATUSES = frozenset({"failed", "crashed"})


def is_error_state(agent_data: dict[str, Any]) -> bool:
    """True when an agent is failed, crashed, or error-driven stopped."""
    status = agent_data.get("status")
    if status in RECOVERABLE_STATUSES:
        return True
    return status == "stopped" and bool(agent_data.get("last_error"))


def render_recovery_details(agent_data: dict[str, Any]) -> Text:
    """Selectable error detail: status, exception type, message, cause, fix."""
    status = str(agent_data.get("status", "failed"))
    last_error = agent_data.get("last_error") or {}
    text = Text()
    text.append(f"Agent {status}", style="bold red")

    err_type = last_error.get("type")
    message = last_error.get("message") or agent_data.get("error_message")
    cause = last_error.get("cause")
    suggested_fix = last_error.get("suggested_fix")

    if err_type:
        text.append(f"  ({err_type})", style="red")
    if message:
        text.append("\n")
        text.append(str(message))
    if cause:
        text.append("\nLikely cause: ", style="bold")
        text.append(str(cause))
    if suggested_fix:
        text.append("\nSuggested fix: ", style="bold")
        text.append(str(suggested_fix))
    return text


def render_recovery_status(agent_data: dict[str, Any]) -> Text:
    """Inline status-bar variant: error detail plus the recovery-options hint."""
    text = render_recovery_details(agent_data)
    text.append("\n\nesc", style="white")
    text.append(" recovery options", style="dim")
    return text


def build_retry_message(last_error: dict[str, Any] | None) -> str:
    """A safe retry instruction that names the prior error without repeating secrets.

    ``last_error['message']`` is already scrubbed when recorded via
    ``AgentCoordinator.record_error``.
    """
    err_type = (last_error or {}).get("type", "an error")
    message = (last_error or {}).get("message", "")
    detail = f" ({message})" if message else ""
    return (
        f"The previous attempt failed with {err_type}{detail}. "
        "Please retry from the last safe point. Re-establish any needed state "
        "before continuing, and do not assume earlier sensitive values are still valid."
    )


def save_for_resume(report_state: ReportState) -> None:
    """Persist run state for a later ``--resume``; leaves ``.state`` intact."""
    report_state.save_run_data(status=PAUSED_STATUS)


def cancel_keep_findings(
    report_state: ReportState,
    coordinator: AgentCoordinator,
    run_dir: Path,
) -> None:
    """Abandon agent replay state but keep findings and the run record.

    Stops further snapshot writes first (so nothing re-creates the files we are
    about to remove), persists findings + run.json under the protected
    ``cancelled_findings_saved`` status, then drops the replay state files.
    """
    coordinator.disable_snapshots()
    report_state.save_run_data(status=CANCELLED_FINDINGS_SAVED_STATUS)
    state_dir = runtime_state_dir(run_dir)
    for name in ("agents.json", "agents.previous.json", "agents.db"):
        with contextlib.suppress(OSError):
            (state_dir / name).unlink(missing_ok=True)

"""Compact tool-call summary extracted from attached SDK sessions.

Used only by ``review_before_finish`` (the QA loop). Never call this from
``finish_scan`` — ``session.get_items()`` materialises the whole SDK session
before local slicing, so the per-agent bound limits *parsing* and *persisted
summaries*, not the SDK load cost. That is acceptable inside the QA review tool
and far too expensive inside a completion guard.
"""

from __future__ import annotations

import json
import logging
import shlex
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from strix.core.scrubbing import scrub_secrets


if TYPE_CHECKING:
    from strix.core.agents import AgentCoordinator


logger = logging.getLogger(__name__)

_MAX_COMMAND_LEN = 200
_MAX_OPTIONS = 12
_MAX_ERROR_LEN = 200


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _exec_basename_and_flags(cmd: str) -> tuple[str | None, list[str]]:
    """Return ``(basename, flag_options)`` for a shell command string.

    Drops leading ``VAR=value`` env assignments and keeps only option flags
    (tokens starting with ``-``); flag *values* are intentionally discarded so
    no secret value survives in the summary.
    """
    cmd = scrub_secrets(cmd)[:_MAX_COMMAND_LEN]
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    basename: str | None = None
    flags: list[str] = []
    for token in tokens:
        if basename is None:
            if "=" in token and not token.startswith("-"):
                continue  # leading env assignment
            basename = PurePosixPath(token).name
            continue
        if token.startswith("-"):
            flag = token.split("=", 1)[0]
            if flag not in flags:
                flags.append(flag)
    return basename, flags[:_MAX_OPTIONS]


def _entry_from_call(
    agent_id: str,
    name: str,
    args: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "agent_id": agent_id,
        "tool_name": name,
        "command": None,
        "key_options": [],
        "status": status,
    }
    if name == "exec_command":
        cmd = args.get("cmd")
        if isinstance(cmd, str) and cmd.strip():
            basename, flags = _exec_basename_and_flags(cmd)
            entry["command"] = basename
            entry["key_options"] = flags
    return entry


def _status_from_output(raw_output: Any) -> str:
    value = raw_output
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return "completed"
    if isinstance(value, dict) and value.get("success") is False:
        return "failed"
    return "completed"


def _parse_session_items(
    agent_id: str,
    items: list[Any],
) -> list[dict[str, Any]]:
    outputs: dict[str, Any] = {}
    for item in items:
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            call_id = str(item.get("call_id") or item.get("id") or "")
            if call_id:
                outputs[call_id] = item.get("output")

    entries: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        name = str(item.get("name") or "tool")
        call_id = str(item.get("call_id") or item.get("id") or "")
        status = _status_from_output(outputs[call_id]) if call_id in outputs else "unknown"
        entries.append(_entry_from_call(agent_id, name, _parse_args(item.get("arguments")), status))
    return entries


async def summarise_agent_tool_history(
    coordinator: AgentCoordinator,
    *,
    per_agent_item_limit: int = 400,
    final_tool_limit: int = 300,
) -> dict[str, Any]:
    """Summarise tool calls across attached agent SDK sessions.

    Returns compact entries plus source-health fields so callers can tell
    "tool history unavailable" (``agents_with_sessions == 0``) apart from
    "available but empty", and full coverage apart from partial coverage
    (non-empty ``extraction_errors``).
    """
    agent_ids = list(coordinator.statuses.keys())
    tool_history: list[dict[str, Any]] = []
    extraction_errors: list[str] = []
    agents_with_sessions = 0

    for agent_id in agent_ids:
        runtime = coordinator.runtimes.get(agent_id)
        session = runtime.session if runtime is not None else None
        if session is None:
            continue
        agents_with_sessions += 1
        try:
            items = list(await session.get_items())
        except Exception as exc:  # noqa: BLE001 - degraded source must not crash the audit
            extraction_errors.append(scrub_secrets(f"{agent_id}: {exc}")[:_MAX_ERROR_LEN])
            logger.debug("tool history extraction failed for %s", agent_id, exc_info=True)
            continue
        # Bound per agent BEFORE parsing/merging. get_items() already materialised
        # the full session; this bounds parsing cost and persisted summary size.
        bounded = items[-per_agent_item_limit:] if per_agent_item_limit > 0 else items
        tool_history.extend(_parse_session_items(agent_id, bounded))

    return {
        "tool_history": tool_history[:final_tool_limit],
        "agents_total": len(agent_ids),
        "agents_with_sessions": agents_with_sessions,
        "extraction_errors": extraction_errors,
    }

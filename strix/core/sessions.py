"""SDK session helpers for Strix agents."""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any, cast

from agents.memory import SQLiteSession


if TYPE_CHECKING:
    from pathlib import Path

    from agents.items import TResponseInputItem
    from agents.memory import Session


def open_agent_session(agent_id: str, path: Path) -> SQLiteSession:
    path.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteSession(session_id=agent_id, db_path=path)


_IMAGE_REJECTED_TEXT = "[image rejected by the model]"


async def strip_all_images_from_session(session: Session) -> bool:
    items = await session.get_items()
    if not items:
        return False

    rebuilt: list[Any] = []
    changed = False
    for item in items:
        item_dict = cast("dict[str, Any]", item) if isinstance(item, dict) else None
        if (
            item_dict is not None
            and item_dict.get("type") == "function_call_output"
            and isinstance(item_dict.get("output"), list)
            and any(
                isinstance(b, dict) and b.get("type") == "input_image" for b in item_dict["output"]
            )
        ):
            rebuilt.append(
                {
                    "type": "function_call_output",
                    "call_id": item_dict.get("call_id"),
                    "output": [{"type": "input_text", "text": _IMAGE_REJECTED_TEXT}],
                },
            )
            changed = True
        else:
            rebuilt.append(item)

    if not changed:
        return False

    rebuilt_items = cast("list[TResponseInputItem]", rebuilt)
    await session.clear_session()
    try:
        await session.add_items(rebuilt_items)
    except Exception:
        with contextlib.suppress(Exception):
            await session.add_items(rebuilt_items)
        raise
    return True


def _tool_call_arguments_are_malformed(raw: Any) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        json.loads(raw or "{}")
    except (json.JSONDecodeError, ValueError):
        return True
    return False


async def repair_malformed_tool_calls_in_session(session: Session) -> bool:
    """Neutralise ``function_call`` history items whose ``arguments`` aren't valid JSON.

    When a model emits a tool call with malformed JSON arguments, the SDK reports
    the parse failure back to the model (the call itself fails, which is handled),
    but the raw call is still stored in history. Replaying it serialises the bad
    ``arguments`` string into the *next* request, which the model endpoint rejects
    with a 400 — poisoning every subsequent turn of the run. The call already
    failed, so its arguments are dead weight: replace them with ``"{}"`` so the
    request serialises cleanly, while leaving the (failed) call and its paired
    output in place for model context. Returns True if anything was repaired.
    """
    items = await session.get_items()
    if not items:
        return False

    rebuilt: list[Any] = []
    changed = False
    for item in items:
        item_dict = cast("dict[str, Any]", item) if isinstance(item, dict) else None
        if (
            item_dict is not None
            and item_dict.get("type") == "function_call"
            and _tool_call_arguments_are_malformed(item_dict.get("arguments"))
        ):
            rebuilt.append({**item_dict, "arguments": "{}"})
            changed = True
        else:
            rebuilt.append(item)

    if not changed:
        return False

    rebuilt_items = cast("list[TResponseInputItem]", rebuilt)
    await session.clear_session()
    try:
        await session.add_items(rebuilt_items)
    except Exception:
        with contextlib.suppress(Exception):
            await session.add_items(rebuilt_items)
        raise
    return True

"""Resume snapshot loading with previous-good fallback.

The coordinator writes ``.state/agents.json`` atomically and keeps the prior
good copy as ``.state/agents.previous.json`` (see ``AgentCoordinator``). On
resume we load the newest valid snapshot, falling back to the previous one when
the current file is corrupt.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)

PREVIOUS_SNAPSHOT_SUFFIX = ".previous.json"


def previous_snapshot_path(agents_path: Path) -> Path:
    """``.state/agents.json`` -> ``.state/agents.previous.json``."""
    return agents_path.with_name(agents_path.name.removesuffix(".json") + PREVIOUS_SNAPSHOT_SUFFIX)


def _read_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("snapshot %s is unreadable/corrupt", path)
        return None
    return data if isinstance(data, dict) else None


def load_latest_snapshot(agents_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load the newest valid snapshot.

    Returns ``(snapshot, warning)``. Tries ``agents.json`` first; if it is
    missing or corrupt, falls back to ``agents.previous.json`` and returns a
    warning that the snapshot may be older than ``agents.db``. Returns
    ``(None, None)`` when no valid snapshot exists.
    """
    current = _read_snapshot(agents_path)
    if current is not None:
        return current, None

    prev_path = previous_snapshot_path(agents_path)
    previous = _read_snapshot(prev_path)
    if previous is not None:
        warning = (
            f"Loaded previous checkpoint {prev_path.name}: the current snapshot "
            f"was missing or corrupt, so topology may be older than agents.db."
        )
        logger.warning(warning)
        return previous, warning

    return None, None

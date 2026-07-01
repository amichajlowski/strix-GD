"""Per-run structured loot store — mirrored to {state_dir}/loot.json (0o600).

Typed, run-wide store for reusable intel (credentials, tokens, cookies,
secrets, endpoints, ids). Mirrors ``strix/tools/notes/tools.py`` lifecycle:
in-memory dict, ``RLock``, atomic persist, hydrate on resume, visible to every
agent in the run.

Secret discipline: raw ``value`` persists to ``loot.json`` only, written 0o600.
``qa_loot_summary`` / ``mask_value`` never emit raw values or source text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents import RunContextWrapper, function_tool

from strix.core.scrubbing import scrub_secrets


logger = logging.getLogger(__name__)

_VALID_LOOT_TYPES = [
    "credential",
    "token",
    "cookie",
    "session",
    "secret",
    "key",
    "hash",
    "endpoint",
    "parameter",
    "user_id",
    "object_id",
    "internal_host",
    "email",
    "other",
]

# Bounds for stored strings and collections.
_MAX_VALUE_LEN = 4096
_MAX_SOURCE_LEN = 512
_MAX_NOTES_LEN = 512
_MAX_TAGS = 32
_MAX_TAG_LEN = 120
_MAX_GET_RESULTS = 200
_MAX_QA_LOOT = 100
# Loot concentrates every secret in the run into one 0o600 file; cap total
# entries so a long or adversarial scan can't grow it without bound.
_MAX_TOTAL_ENTRIES = 5000
_MAX_QA_TAG_LEN = 60

_loot_storage: dict[str, dict[str, Any]] = {}
_loot_lock = threading.RLock()
_loot_path: Path | None = None


def hydrate_loot_from_disk(state_dir: Path) -> None:
    global _loot_path  # noqa: PLW0603
    _loot_path = state_dir / "loot.json"
    with _loot_lock:
        _loot_storage.clear()
        if not _loot_path.exists():
            return
        try:
            data = json.loads(_loot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception(
                "loot.json at %s is unreadable; starting with empty loot",
                _loot_path,
            )
            return
        if not isinstance(data, dict):
            return
        _loot_storage.update(
            {
                lid: entry
                for lid, entry in data.items()
                if isinstance(lid, str) and isinstance(entry, dict)
            }
        )
        logger.info(
            "loot hydrated from %s (%d entr(ies))",
            _loot_path,
            len(_loot_storage),
        )


def _persist() -> None:
    path = _loot_path
    if path is None:
        return
    try:
        payload = json.dumps(_loot_storage, ensure_ascii=False, default=str)
        path.parent.mkdir(parents=True, exist_ok=True)
        with (
            _loot_lock,
            tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp,
        ):
            tmp.write(payload)
            tmp_path = Path(tmp.name)
            # Secret discipline: loot concentrates every secret in the run into
            # one file — tighten to 0o600 BEFORE the atomic replace so the final
            # loot.json is only readable by the owner.
            os.fchmod(tmp.fileno(), 0o600)
        tmp_path.replace(path)
    except Exception:
        logger.exception("loot persist to %s failed", path)


def _bound_tags(tags: list[str] | None) -> list[str]:
    """Dedup (order-preserving), bound each tag, cap the count."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in tags or []:
        tag = str(raw)[:_MAX_TAG_LEN]
        if tag and tag not in seen:
            seen.add(tag)
            result.append(tag)
        if len(result) >= _MAX_TAGS:
            break
    return result


def _validate_record_input(loot_type: str, value: str, source: str) -> dict[str, Any] | None:
    """Return an error result if inputs are invalid, else None."""
    if loot_type not in _VALID_LOOT_TYPES:
        return {
            "success": False,
            "error": f"Invalid loot_type. Must be one of: {', '.join(_VALID_LOOT_TYPES)}",
            "loot_id": None,
        }
    if not value or not value.strip():
        return {"success": False, "error": "value cannot be empty", "loot_id": None}
    if not source or not source.strip():
        return {"success": False, "error": "source cannot be empty", "loot_id": None}
    return None


def _record_loot_impl(
    loot_type: str,
    value: str,
    source: str,
    scope: str | None = None,
    tags: list[str] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    with _loot_lock:
        try:
            if err := _validate_record_input(loot_type, value, source):
                return err

            value = value[:_MAX_VALUE_LEN]
            source = source[:_MAX_SOURCE_LEN]
            new_tags = _bound_tags(tags)
            bounded_notes = notes[:_MAX_NOTES_LEN] if notes else None
            timestamp = datetime.now(UTC).isoformat()

            # Dedup on (loot_type, value, scope): merge instead of duplicating.
            for existing_id, entry in _loot_storage.items():
                if (
                    entry.get("loot_type") == loot_type
                    and entry.get("value") == value
                    and entry.get("scope") == scope
                ):
                    entry["tags"] = _bound_tags([*entry.get("tags", []), *new_tags])
                    existing_source = str(entry.get("source", ""))
                    if source and source not in existing_source:
                        merged = f"{existing_source}; {source}" if existing_source else source
                        entry["source"] = merged[:_MAX_SOURCE_LEN]
                    if bounded_notes and not entry.get("notes"):
                        entry["notes"] = bounded_notes
                    entry["updated_at"] = timestamp
                    _persist()
                    return {
                        "success": True,
                        "loot_id": existing_id,
                        "deduped": True,
                        "total_count": len(_loot_storage),
                    }

            if len(_loot_storage) >= _MAX_TOTAL_ENTRIES:
                return {
                    "success": False,
                    "error": (
                        f"Loot store is full ({_MAX_TOTAL_ENTRIES} entries); "
                        "delete stale loot before recording more"
                    ),
                    "loot_id": None,
                }

            loot_id = str(uuid.uuid4())[:6]
            _loot_storage[loot_id] = {
                "loot_type": loot_type,
                "value": value,
                "source": source,
                "scope": scope,
                "tags": new_tags,
                "notes": bounded_notes,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        except (ValueError, TypeError) as e:
            return {"success": False, "error": f"Failed to record loot: {e}", "loot_id": None}
        else:
            _persist()
            return {
                "success": True,
                "loot_id": loot_id,
                "deduped": False,
                "total_count": len(_loot_storage),
            }


def _get_loot_impl(
    loot_type: str | None = None,
    scope: str | None = None,
    tags: list[str] | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    with _loot_lock:
        try:
            filtered: list[dict[str, Any]] = []
            for loot_id, entry in _loot_storage.items():
                if loot_type and entry.get("loot_type") != loot_type:
                    continue
                if scope and entry.get("scope") != scope:
                    continue
                if tags:
                    entry_tags = entry.get("tags", [])
                    if not any(tag in entry_tags for tag in tags):
                        continue
                if search:
                    needle = search.lower()
                    haystack = " ".join(
                        str(entry.get(field, "") or "")
                        for field in ("value", "source", "notes")
                    ).lower()
                    if needle not in haystack:
                        continue
                item = entry.copy()
                item["loot_id"] = loot_id
                filtered.append(item)

            filtered.sort(
                key=lambda x: x.get("updated_at") or x.get("created_at") or "",
                reverse=True,
            )
            results = filtered[:_MAX_GET_RESULTS]
        except (ValueError, TypeError) as e:
            return {
                "success": False,
                "error": f"Failed to get loot: {e}",
                "results": [],
                "filtered_count": 0,
                "total_count": 0,
            }
        return {
            "success": True,
            "results": results,
            "filtered_count": len(filtered),
            "total_count": len(_loot_storage),
        }


def _delete_loot_impl(loot_id: str) -> dict[str, Any]:
    with _loot_lock:
        try:
            if loot_id not in _loot_storage:
                return {"success": False, "error": f"Loot with ID '{loot_id}' not found"}
            del _loot_storage[loot_id]
        except (ValueError, TypeError) as e:
            return {"success": False, "error": f"Failed to delete loot: {e}"}
        else:
            _persist()
            return {
                "success": True,
                "loot_id": loot_id,
                "message": f"Loot '{loot_id}' deleted successfully",
                "total_count": len(_loot_storage),
            }


def mask_value(value: str) -> str:
    """Return a non-reversible hint for a raw loot value — never the value itself."""
    return f"XXXX (len={len(value)})"


def qa_loot_summary(limit: int = _MAX_QA_LOOT) -> dict[str, Any]:
    """Compact loot view for the QA review.

    Returns persisted-safe ``refs`` (loot id, loot_type, scope, scrubbed/bounded
    tags only — no raw value, no source text) and in-memory ``signals``
    (lowercased loot_type/tags) that rule evaluation may inspect but which must
    never be persisted verbatim.
    """
    refs: list[dict[str, Any]] = []
    signals: list[str] = []
    with _loot_lock:
        for loot_id, entry in list(_loot_storage.items())[: max(0, limit)]:
            tags = [scrub_secrets(str(t))[:_MAX_QA_TAG_LEN] for t in entry.get("tags", []) or []]
            refs.append(
                {
                    "loot_id": loot_id,
                    "loot_type": entry.get("loot_type", "other"),
                    "scope": entry.get("scope"),
                    "tags": tags,
                }
            )
            signals.append(str(entry.get("loot_type", "")).lower())
            signals.extend(str(t).lower() for t in entry.get("tags", []) or [])
    return {"refs": refs, "signals": [s for s in signals if s]}


@function_tool(timeout=30)
async def record_loot(
    ctx: RunContextWrapper,
    loot_type: str,
    value: str,
    source: str,
    scope: str | None = None,
    tags: list[str] | None = None,
    notes: str | None = None,
) -> str:
    """Record a reusable artifact the moment you find it.

    Call this whenever you harvest anything reusable — a credential, token,
    session cookie, API key, secret, internal hostname, interesting endpoint,
    or an object/user id useful for IDOR. Recording it makes it available to
    every other agent in the scan, so isolated findings can chain into
    high-impact exploits.

    Duplicates on ``(loot_type, value, scope)`` are merged, not re-added.

    Args:
        loot_type: One of credential, token, cookie, session, secret, key,
            hash, endpoint, parameter, user_id, object_id, internal_host,
            email, other.
        value: The raw reusable value (kept full for reuse).
        source: Where it came from (free text).
        scope: Optional host/app it belongs to.
        tags: Optional free-form tags.
        notes: Optional short context.
    """
    return json.dumps(
        await asyncio.to_thread(
            _record_loot_impl, loot_type, value, source, scope, tags, notes
        ),
        ensure_ascii=False,
        default=str,
    )


@function_tool(timeout=30)
async def get_loot(
    ctx: RunContextWrapper,
    loot_type: str | None = None,
    scope: str | None = None,
    tags: list[str] | None = None,
    search: str | None = None,
) -> str:
    """Pull existing loot before testing new surface — reuse is how chains form.

    Before testing a new endpoint or spinning up a new attack, pull existing
    loot and try it: reuse harvested creds/tokens/cookies for auth, known ids
    for access-control tests, discovered endpoints as new surface. Returns full
    raw values (agents need them to reuse). Filters compose (AND across
    loot_type/scope/tags, plus a search substring over value/source/notes).

    Args:
        loot_type: Filter by loot type.
        scope: Filter by scope (host/app).
        tags: Filter to entries having any of these tags.
        search: Substring match against value/source/notes.
    """
    return json.dumps(
        await asyncio.to_thread(_get_loot_impl, loot_type, scope, tags, search),
        ensure_ascii=False,
        default=str,
    )


@function_tool(timeout=30)
async def delete_loot(ctx: RunContextWrapper, loot_id: str) -> str:
    """Delete a loot entry by its id.

    Args:
        loot_id: Loot id to delete.
    """
    return json.dumps(
        await asyncio.to_thread(_delete_loot_impl, loot_id),
        ensure_ascii=False,
        default=str,
    )

"""Per-run adaptive-audit working-thesis store — mirrored to {state_dir}/audit_state.json.

A single per-run *working thesis* document shared by every agent: a bounded
thesis paragraph, a set of assumptions (with confidence + supersede history),
and a list of prioritised leads. This is the synthesis/direction layer on top
of the raw stores (loot, target_profile, notes).

Mirrors ``strix/tools/notes/tools.py`` lifecycle (in-memory dict, ``RLock``,
atomic persist, hydrate on resume) except the store holds one document, not a
keyed collection.

Secret discipline: ``audit_state`` is derived intel only — never a raw secret
value (reference loot by id). ``update_audit_state`` runs ``scrub_secrets`` over
its free-text fields at write time as a cheap backstop. Notes-style atomic
persist (0644); no raw secrets, so no 0o600.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents import RunContextWrapper, function_tool

from strix.core.scrubbing import scrub_secrets


logger = logging.getLogger(__name__)

_VALID_CONFIDENCE = ["low", "medium", "high"]
_VALID_PRIORITY = ["low", "medium", "high"]
_VALID_LEAD_STATUS = ["open", "in_progress", "done", "dropped"]

# Bounds (see 02 §Bounds).
_MAX_THESIS_LEN = 1000
_MAX_TEXT_LEN = 512
_MAX_REF_LEN = 64
_MAX_REFS = 32
_MAX_ACTIVE_ASSUMPTIONS = 200
_MAX_LEADS = 200
_MAX_TOTAL_ASSUMPTIONS = 1000
_MAX_QA_SUMMARY = 100

# Lead statuses shown first in the get_audit_state view.
_LEAD_STATUS_ORDER = {"open": 0, "in_progress": 1, "done": 2, "dropped": 3}

_audit_state: dict[str, Any] = {}
_audit_state_lock = threading.RLock()
_audit_state_path: Path | None = None


def _empty_doc() -> dict[str, Any]:
    return {"thesis": "", "assumptions": {}, "leads": {}, "updated_at": None}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _bound_refs(refs: list[str] | None) -> list[str]:
    """Bound each ref length and cap the count (ids only)."""
    result: list[str] = []
    for raw in refs or []:
        ref = str(raw)[:_MAX_REF_LEN]
        if ref:
            result.append(ref)
        if len(result) >= _MAX_REFS:
            break
    return result


def hydrate_audit_state_from_disk(state_dir: Path) -> None:
    global _audit_state_path  # noqa: PLW0603
    _audit_state_path = state_dir / "audit_state.json"
    with _audit_state_lock:
        _audit_state.clear()
        _audit_state.update(_empty_doc())
        if not _audit_state_path.exists():
            return
        try:
            data = json.loads(_audit_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception(
                "audit_state.json at %s is unreadable; starting with empty document",
                _audit_state_path,
            )
            return
        if not isinstance(data, dict):
            return
        _audit_state["thesis"] = str(data.get("thesis", "") or "")[:_MAX_THESIS_LEN]
        assumptions = data.get("assumptions")
        _audit_state["assumptions"] = (
            {k: v for k, v in assumptions.items() if isinstance(k, str) and isinstance(v, dict)}
            if isinstance(assumptions, dict)
            else {}
        )
        leads = data.get("leads")
        _audit_state["leads"] = (
            {k: v for k, v in leads.items() if isinstance(k, str) and isinstance(v, dict)}
            if isinstance(leads, dict)
            else {}
        )
        _audit_state["updated_at"] = data.get("updated_at")
        logger.info(
            "audit_state hydrated from %s (%d assumption(s), %d lead(s))",
            _audit_state_path,
            len(_audit_state["assumptions"]),
            len(_audit_state["leads"]),
        )


def _persist() -> None:
    path = _audit_state_path
    if path is None:
        return
    try:
        with _audit_state_lock:
            payload = json.dumps(_audit_state, ensure_ascii=False, default=str)
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp.write(payload)
                tmp_path = Path(tmp.name)
        tmp_path.replace(path)
    except Exception:
        logger.exception("audit_state persist to %s failed", path)


def _snapshot_audit_state() -> dict[str, Any]:
    """Deep-ish copy of the module document under lock (lock-safe reads, R3)."""
    with _audit_state_lock:
        if not _audit_state:
            return _empty_doc()
        return copy.deepcopy(_audit_state)


# --------------------------------------------------------------------------- #
# Bound enforcement (applied to the returned new state)
# --------------------------------------------------------------------------- #


def _enforce_assumption_bounds(assumptions: dict[str, Any]) -> dict[str, Any]:
    """Cap active assumptions and total (active+superseded), evicting oldest
    superseded first, then oldest active if still over the active cap.
    """

    def _created(entry: dict[str, Any]) -> str:
        return str(entry.get("created_at", ""))

    result = dict(assumptions)

    # Total cap: drop oldest superseded first.
    if len(result) > _MAX_TOTAL_ASSUMPTIONS:
        superseded = sorted(
            (aid for aid, e in result.items() if e.get("status") == "superseded"),
            key=lambda aid: _created(result[aid]),
        )
        for aid in superseded:
            if len(result) <= _MAX_TOTAL_ASSUMPTIONS:
                break
            del result[aid]
        # If still over (all active), drop oldest active.
        if len(result) > _MAX_TOTAL_ASSUMPTIONS:
            actives = sorted(result, key=lambda aid: _created(result[aid]))
            for aid in actives:
                if len(result) <= _MAX_TOTAL_ASSUMPTIONS:
                    break
                del result[aid]

    # Active cap: drop oldest active beyond the limit.
    active = sorted(
        (aid for aid, e in result.items() if e.get("status") == "active"),
        key=lambda aid: _created(result[aid]),
    )
    if len(active) > _MAX_ACTIVE_ASSUMPTIONS:
        for aid in active[: len(active) - _MAX_ACTIVE_ASSUMPTIONS]:
            del result[aid]

    return result


def _enforce_lead_bounds(leads: dict[str, Any]) -> dict[str, Any]:
    """Cap total leads, dropping oldest (by created_at) first."""
    if len(leads) <= _MAX_LEADS:
        return dict(leads)
    ordered = sorted(leads, key=lambda lid: str(leads[lid].get("created_at", "")))
    keep = ordered[len(ordered) - _MAX_LEADS :]
    return {lid: leads[lid] for lid in keep}


# --------------------------------------------------------------------------- #
# Pure helpers (take state dict, return NEW dict — never mutate the argument)
# --------------------------------------------------------------------------- #


def _apply_assumption(
    state: dict[str, Any],
    *,
    text: str,
    confidence: str,
    supersedes: str | None,
    reason: str | None,
    refs: list[str] | None,
) -> tuple[dict[str, Any], str]:
    """Return (new_state, assumption_id). Handles the supersede transition. Pure.

    Raises ``ValueError`` on unknown ``confidence`` or an unknown /
    already-superseded ``supersedes`` target.
    """
    if confidence not in _VALID_CONFIDENCE:
        raise ValueError(
            f"Invalid confidence. Must be one of: {', '.join(_VALID_CONFIDENCE)}"
        )

    assumptions: dict[str, Any] = copy.deepcopy(state.get("assumptions", {}))

    if supersedes is not None:
        target = assumptions.get(supersedes)
        if target is None:
            raise ValueError(f"supersedes points at unknown assumption id '{supersedes}'")
        if target.get("status") == "superseded":
            raise ValueError(
                f"assumption '{supersedes}' is already superseded and cannot be superseded again"
            )

    timestamp = _now()
    assumption_id = str(uuid.uuid4())[:6]

    assumptions[assumption_id] = {
        "assumption_id": assumption_id,
        "text": scrub_secrets(text)[:_MAX_TEXT_LEN],
        "confidence": confidence,
        "status": "active",
        "supersedes": supersedes,
        "superseded_by": None,
        "reason": scrub_secrets(reason)[:_MAX_TEXT_LEN] if reason else None,
        "refs": _bound_refs(refs),
        "created_at": timestamp,
        "updated_at": timestamp,
    }

    if supersedes is not None:
        old = dict(assumptions[supersedes])
        old["status"] = "superseded"
        old["superseded_by"] = assumption_id
        old["updated_at"] = timestamp
        assumptions[supersedes] = old

    assumptions = _enforce_assumption_bounds(assumptions)

    new_state = dict(state)
    new_state["assumptions"] = assumptions
    new_state["updated_at"] = timestamp
    return new_state, assumption_id


def _apply_lead(
    state: dict[str, Any],
    *,
    text: str,
    priority: str,
    rationale: str | None,
    refs: list[str] | None,
) -> tuple[dict[str, Any], str]:
    """Return (new_state, lead_id) for a new open lead. Pure. Raises on bad enum."""
    if priority not in _VALID_PRIORITY:
        raise ValueError(f"Invalid priority. Must be one of: {', '.join(_VALID_PRIORITY)}")

    leads: dict[str, Any] = copy.deepcopy(state.get("leads", {}))
    timestamp = _now()
    lead_id = str(uuid.uuid4())[:6]

    leads[lead_id] = {
        "lead_id": lead_id,
        "text": scrub_secrets(text)[:_MAX_TEXT_LEN],
        "priority": priority,
        "status": "open",
        "rationale": scrub_secrets(rationale)[:_MAX_TEXT_LEN] if rationale else None,
        "refs": _bound_refs(refs),
        "created_at": timestamp,
        "updated_at": timestamp,
    }

    leads = _enforce_lead_bounds(leads)

    new_state = dict(state)
    new_state["leads"] = leads
    new_state["updated_at"] = timestamp
    return new_state, lead_id


def _update_lead(
    state: dict[str, Any],
    *,
    lead_id: str,
    status: str | None,
    priority: str | None,
) -> dict[str, Any]:
    """Return new_state with the lead updated. Pure. Raises on unknown id / bad enum."""
    leads: dict[str, Any] = state.get("leads", {})
    if lead_id not in leads:
        raise ValueError(f"unknown lead_id '{lead_id}'")
    if status is not None and status not in _VALID_LEAD_STATUS:
        raise ValueError(
            f"Invalid lead_status. Must be one of: {', '.join(_VALID_LEAD_STATUS)}"
        )
    if priority is not None and priority not in _VALID_PRIORITY:
        raise ValueError(f"Invalid priority. Must be one of: {', '.join(_VALID_PRIORITY)}")

    leads = copy.deepcopy(leads)
    entry = dict(leads[lead_id])
    if status is not None:
        entry["status"] = status
    if priority is not None:
        entry["priority"] = priority
    entry["updated_at"] = _now()
    leads[lead_id] = entry

    new_state = dict(state)
    new_state["leads"] = leads
    new_state["updated_at"] = entry["updated_at"]
    return new_state


# --------------------------------------------------------------------------- #
# QA summary (ids/enums only in refs; free text only in in-memory signals)
# --------------------------------------------------------------------------- #


def qa_audit_summary(limit: int = _MAX_QA_SUMMARY) -> dict[str, Any]:
    """Compact audit view for the QA review.

    ``refs`` carry ids/enums ONLY (no free text) — they are persisted into the
    QA review, matching ``qa_loot_summary``. Open/in_progress leads →
    {lead_id, priority, status}; active assumptions → {assumption_id, confidence}.
    ``signals`` are lowercased + ``scrub_secrets``-cleaned thesis/lead/assumption
    text for rule inspection only — in-memory, never persisted.

    Null-safe on an empty/missing/partial document; never raises (R1).
    """
    refs: list[dict[str, Any]] = []
    signals: list[str] = []
    cap = max(0, limit)
    with _audit_state_lock:
        doc = _audit_state if _audit_state else {}
        thesis = str(doc.get("thesis", "") or "")
        if thesis:
            signals.append(scrub_secrets(thesis).lower())

        leads = doc.get("leads") or {}
        if isinstance(leads, dict):
            for entry in list(leads.values())[:cap]:
                if not isinstance(entry, dict):
                    continue
                if entry.get("status") in ("open", "in_progress"):
                    refs.append(
                        {
                            "lead_id": entry.get("lead_id"),
                            "priority": entry.get("priority"),
                            "status": entry.get("status"),
                        }
                    )
                signals.append(scrub_secrets(str(entry.get("text", "") or "")).lower())

        assumptions = doc.get("assumptions") or {}
        if isinstance(assumptions, dict):
            for entry in list(assumptions.values())[:cap]:
                if not isinstance(entry, dict):
                    continue
                if entry.get("status") == "active":
                    refs.append(
                        {
                            "assumption_id": entry.get("assumption_id"),
                            "confidence": entry.get("confidence"),
                        }
                    )
                signals.append(scrub_secrets(str(entry.get("text", "") or "")).lower())

    return {"refs": refs, "signals": [s for s in signals if s]}


# --------------------------------------------------------------------------- #
# Sync impls (tool wraps via asyncio.to_thread) — under lock + persist
# --------------------------------------------------------------------------- #


def _counts() -> dict[str, int]:
    assumptions = _audit_state.get("assumptions", {}) or {}
    leads = _audit_state.get("leads", {}) or {}
    active = sum(1 for e in assumptions.values() if e.get("status") == "active")
    open_leads = sum(1 for e in leads.values() if e.get("status") in ("open", "in_progress"))
    return {
        "assumptions_active": active,
        "assumptions_total": len(assumptions),
        "leads_open": open_leads,
        "leads_total": len(leads),
    }


def _get_audit_state_impl(include_superseded: bool = False) -> dict[str, Any]:
    """Return the working thesis. Never errors on an empty store."""
    with _audit_state_lock:
        doc = _audit_state if _audit_state else {}
        thesis = str(doc.get("thesis", "") or "")

        assumptions_src = doc.get("assumptions") or {}
        assumptions = {
            aid: entry
            for aid, entry in assumptions_src.items()
            if include_superseded or entry.get("status") != "superseded"
        }

        leads_src = doc.get("leads") or {}
        leads_items = sorted(
            leads_src.items(),
            key=lambda kv: (
                _LEAD_STATUS_ORDER.get(kv[1].get("status", "open"), 99),
                str(kv[1].get("created_at", "")),
            ),
        )
        leads = dict(leads_items)

        return {
            "success": True,
            "thesis": thesis,
            "assumptions": assumptions,
            "leads": leads,
            "updated_at": doc.get("updated_at"),
        }


def _update_audit_state_impl(
    *,
    thesis: str | None = None,
    assumption: str | None = None,
    confidence: str | None = None,
    supersedes: str | None = None,
    reason: str | None = None,
    lead: str | None = None,
    priority: str | None = None,
    lead_id: str | None = None,
    lead_status: str | None = None,
    refs: list[str] | None = None,
) -> dict[str, Any]:
    """Apply exactly the provided pieces atomically. Scrub free text at write time."""
    with _audit_state_lock:
        if not _audit_state:
            _audit_state.update(_empty_doc())

        # Validate companion-field requirements before mutating anything.
        if assumption is not None and confidence is None:
            return {
                "success": False,
                "error": "confidence is required when adding an assumption",
                "counts": _counts(),
            }
        if lead is not None and priority is None:
            return {
                "success": False,
                "error": "priority is required when adding a lead",
                "counts": _counts(),
            }

        working: dict[str, Any] = _snapshot_audit_state()
        result: dict[str, Any] = {"success": True}

        try:
            if thesis is not None:
                working = dict(working)
                working["thesis"] = scrub_secrets(thesis)[:_MAX_THESIS_LEN]
                working["updated_at"] = _now()

            if assumption is not None:
                working, assumption_id = _apply_assumption(
                    working,
                    text=assumption,
                    confidence=confidence,  # type: ignore[arg-type]
                    supersedes=supersedes,
                    reason=reason,
                    refs=refs,
                )
                result["assumption_id"] = assumption_id

            if lead is not None:
                working, new_lead_id = _apply_lead(
                    working,
                    text=lead,
                    priority=priority,  # type: ignore[arg-type]
                    rationale=reason,
                    refs=refs,
                )
                result["lead_id"] = new_lead_id

            if lead_id is not None:
                working = _update_lead(
                    working,
                    lead_id=lead_id,
                    status=lead_status,
                    priority=priority if lead is None else None,
                )
                result["lead_id"] = lead_id
        except ValueError as e:
            return {"success": False, "error": str(e), "counts": _counts()}

        # Commit atomically: replace the module document in place.
        _audit_state.clear()
        _audit_state.update(working)
        _persist()

        result["counts"] = _counts()
        return result


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@function_tool(timeout=30)
async def get_audit_state(ctx: RunContextWrapper, include_superseded: bool = False) -> str:
    """Read the shared working thesis before starting a new surface.

    Returns the thesis, active assumptions (add ``include_superseded=True`` for
    the full audit trail), open/in_progress leads first, and ``updated_at``.

    Args:
        include_superseded: Include superseded assumptions in the view.
    """
    return json.dumps(
        await asyncio.to_thread(_get_audit_state_impl, include_superseded),
        ensure_ascii=False,
        default=str,
    )


@function_tool(timeout=30)
async def update_audit_state(
    ctx: RunContextWrapper,
    thesis: str | None = None,
    assumption: str | None = None,
    confidence: str | None = None,
    supersedes: str | None = None,
    reason: str | None = None,
    lead: str | None = None,
    priority: str | None = None,
    lead_id: str | None = None,
    lead_status: str | None = None,
    refs: list[str] | None = None,
) -> str:
    """Update the shared working thesis: thesis and/or one assumption and/or one lead.

    Overload wart: a lead's rationale is passed through the shared ``reason``
    param (the same param that carries an assumption's supersede reason). Write
    EITHER an assumption OR a lead per call when ``reason`` is set, not both.

    - ``thesis`` replaces the thesis.
    - ``assumption`` (+ ``confidence``, optional ``supersedes``/``reason``/``refs``)
      adds an assumption. When ``supersedes`` is set, the target is marked
      superseded and linked both ways; unknown/already-superseded ids error.
    - ``lead`` (+ ``priority``, optional rationale via ``reason``, ``refs``) adds
      an open lead.
    - ``lead_id`` (+ ``lead_status``, optional ``priority``) updates a lead.

    Args:
        thesis: New one-paragraph thesis.
        assumption: Assumption text to add.
        confidence: Required with ``assumption`` — low | medium | high.
        supersedes: assumption_id this one replaces.
        reason: Supersede reason (assumption) OR lead rationale — one per call.
        lead: Lead text to add.
        priority: Required with ``lead`` — low | medium | high.
        lead_id: Existing lead to update.
        lead_status: New lead status — open | in_progress | done | dropped.
        refs: Evidence ids (loot_id/note_id) for the assumption/lead written.
    """
    return json.dumps(
        await asyncio.to_thread(
            _update_audit_state_impl,
            thesis=thesis,
            assumption=assumption,
            confidence=confidence,
            supersedes=supersedes,
            reason=reason,
            lead=lead,
            priority=priority,
            lead_id=lead_id,
            lead_status=lead_status,
            refs=refs,
        ),
        ensure_ascii=False,
        default=str,
    )

"""Feature 2 — the adaptive-audit reflection loop (a code path, not an agent).

At each specialist-child completion boundary a **reflection step** reads the
shared blackboard, reconciles it against the prior thesis, and writes the
revised thesis / assumptions / leads back into ``audit_state`` via the
Feature-1 pure helpers. The root then reads ``audit_state`` and steers.

This module is a dedicated, deterministic, unit-testable code path (see
``Specs/adaptive-audit/03-strategist-loop.md`` and ``05-review-findings.md``):

* ``build_reflection_input`` — pure snapshot -> chat messages (no I/O).
* ``apply_reflection`` — validate + apply a model delta to ``audit_state``
  under ``_audit_state_lock`` (R6); ignores malformed items, never raises.
* ``run_reflection`` — impure runner: lock-safe snapshot (R3), one model call
  mirroring ``report/dedupe.py`` (R2/R4), tolerant JSON parse (one retry, else
  clean skip), record usage. Never raises into the caller. Budget-aware.
* ``_schedule_reflection`` — module-level single-flight scheduler that
  coalesces a burst of near-simultaneous completions into <=1 extra run.

The model call mirrors ``strix/report/dedupe.py:189-214`` exactly: no raw
``litellm`` call, no hand-rolled model-name mapping, structured output via a
prompt-instructed JSON object parsed tolerantly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, cast

from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing

from strix.config import load_settings
from strix.config.models import (
    StrixProvider,
    configure_sdk_model_defaults,
    model_retry_settings_from_config,
)
from strix.core.scrubbing import scrub_secrets
from strix.report.dedupe import _extract_text
from strix.report.state import get_global_report_state
from strix.tools.audit_state.tools import (
    _MAX_THESIS_LEN,
    _apply_assumption,
    _apply_lead,
    _audit_state,
    _audit_state_lock,
    _empty_doc,
    _get_audit_state_impl,
    _now,
    _persist,
    _snapshot_audit_state,
    _update_lead,
    qa_audit_summary,
)
from strix.tools.loot.tools import qa_loot_summary
from strix.tools.notes.tools import qa_notes_summary
from strix.tools.qa_loop.rules import evaluate_qa_gaps
from strix.tools.target_profile.tools import _get_target_profile_impl


if TYPE_CHECKING:
    from agents.items import ModelResponse


logger = logging.getLogger(__name__)

_VALID_CONFIDENCE = frozenset({"low", "medium", "high"})
_VALID_PRIORITY = frozenset({"low", "medium", "high"})
_VALID_LEAD_STATUS = frozenset({"open", "in_progress", "done", "dropped"})

_MAX_SNAPSHOT_LOOT = 50
_MAX_SNAPSHOT_LEADS = 50
_MAX_SNAPSHOT_NOTES = 50
_MAX_SNAPSHOT_GAPS = 20


REFLECTION_PROMPT = """You are the reflection step of an autonomous security audit.

You are given a JSON snapshot of the current audit blackboard: the prior working
thesis, the active assumptions, the prioritised leads, references to collected
loot (BY ID ONLY), the target profile, a digest of recent traffic (if any),
findings-note titles, and the currently open QA gaps.

Your job is to reconcile the evidence against the prior thesis and produce a
revised working thesis, plus any assumption / lead changes. Rules:

- Reconcile, do NOT rebuild. Keep the prior thesis where it still holds. When an
  assumption is now contradicted or refined, SUPERSEDE it: emit a new assumption
  whose ``supersedes`` is the old ``assumption_id`` and give a short ``reason``.
- Revise the thesis to a single tight paragraph describing the most promising
  direction of the audit right now.
- Add or reprioritise leads that the evidence now justifies. Use ``high`` only
  for leads worth blocking the finish gate on. Reference loot BY ``loot_id``,
  NEVER by raw value.
- If nothing material changed, return an empty delta (null thesis, empty lists).

Respond with ONLY a single JSON object and nothing else (no prose, no code
fences), matching exactly this schema:

{
  "thesis": "revised one-paragraph thesis, or null if unchanged",
  "assumptions": [
    {"text": "...", "confidence": "low|medium|high",
     "supersedes": "assumption_id or null", "reason": "why superseded, or null",
     "refs": ["loot_id", ...]}
  ],
  "leads": [
    {"text": "...", "priority": "low|medium|high",
     "rationale": "why, or null", "refs": ["loot_id", ...]}
  ],
  "lead_updates": [
    {"lead_id": "existing lead id", "status": "open|in_progress|done|dropped",
     "priority": "low|medium|high"}
  ]
}

Use a low temperature. Output ONLY the JSON object."""


# --------------------------------------------------------------------------- #
# Pure: build the reflection chat messages from a blackboard snapshot
# --------------------------------------------------------------------------- #


def build_reflection_input(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    """Pure. Assemble the chat messages for the reflection call from a
    blackboard snapshot. No I/O, deterministic.

    The snapshot carries ids/enums only (loot refs by id, lead refs by id,
    QA gap ids) — this function performs no raw-value expansion of any kind, so
    whatever the caller passes in is what appears in the rendered messages.
    """
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
    return [
        {"role": "system", "content": REFLECTION_PROMPT},
        {
            "role": "user",
            "content": (
                "Reconcile the audit blackboard below against the prior thesis and "
                "return ONLY the JSON delta described in the system prompt.\n\n"
                f"{payload}"
            ),
        },
    ]


# --------------------------------------------------------------------------- #
# Pure-ish: validate + apply a model delta to audit_state (under the store lock)
# --------------------------------------------------------------------------- #


def _apply_thesis(working: dict[str, Any], thesis: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(thesis, str) or not thesis.strip():
        return working, False
    new_working = dict(working)
    new_working["thesis"] = scrub_secrets(thesis)[:_MAX_THESIS_LEN]
    new_working["updated_at"] = _now()
    return new_working, True


def _try_apply_assumption(working: dict[str, Any], item: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(item, dict):
        return working, False
    text = item.get("text")
    confidence = item.get("confidence")
    if not isinstance(text, str) or not text.strip():
        return working, False
    if confidence not in _VALID_CONFIDENCE:
        return working, False
    supersedes = item.get("supersedes")
    if supersedes is not None and not isinstance(supersedes, str):
        return working, False
    reason = item.get("reason") if isinstance(item.get("reason"), str) else None
    refs = item.get("refs") if isinstance(item.get("refs"), list) else None
    try:
        new_working, _ = _apply_assumption(
            working,
            text=text,
            confidence=confidence,
            supersedes=supersedes,
            reason=reason,
            refs=refs,
        )
    except ValueError:
        return working, False
    return new_working, True


def _try_apply_lead(working: dict[str, Any], item: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(item, dict):
        return working, False
    text = item.get("text")
    priority = item.get("priority")
    if not isinstance(text, str) or not text.strip():
        return working, False
    if priority not in _VALID_PRIORITY:
        return working, False
    rationale = item.get("rationale") if isinstance(item.get("rationale"), str) else None
    refs = item.get("refs") if isinstance(item.get("refs"), list) else None
    try:
        new_working, _ = _apply_lead(
            working,
            text=text,
            priority=priority,
            rationale=rationale,
            refs=refs,
        )
    except ValueError:
        return working, False
    return new_working, True


def _try_apply_lead_update(working: dict[str, Any], item: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(item, dict):
        return working, False
    lead_id = item.get("lead_id")
    if not isinstance(lead_id, str) or not lead_id:
        return working, False
    status = item.get("status")
    priority = item.get("priority")
    if status is not None and status not in _VALID_LEAD_STATUS:
        return working, False
    if priority is not None and priority not in _VALID_PRIORITY:
        return working, False
    try:
        new_working = _update_lead(
            working,
            lead_id=lead_id,
            status=status,
            priority=priority,
        )
    except ValueError:
        return working, False
    return new_working, True


def apply_reflection(result: Any) -> dict[str, Any]:
    """Validate the model's structured delta and apply it to ``audit_state``
    via the Feature-1 pure helpers, holding ``_audit_state_lock`` while mutating
    the module document and persisting (R6). Ignores malformed items (bad enum,
    missing field, unknown supersede/lead id) WITHOUT raising.

    Returns a summary of what changed.
    """
    summary = {
        "thesis_changed": False,
        "assumptions_added": 0,
        "leads_added": 0,
        "lead_updates": 0,
    }
    if not isinstance(result, dict):
        return summary

    with _audit_state_lock:
        if not _audit_state:
            _audit_state.update(_empty_doc())

        working: dict[str, Any] = _snapshot_audit_state()

        working, thesis_changed = _apply_thesis(working, result.get("thesis"))
        summary["thesis_changed"] = thesis_changed

        for item in result.get("assumptions") or []:
            working, applied = _try_apply_assumption(working, item)
            if applied:
                summary["assumptions_added"] += 1

        for item in result.get("leads") or []:
            working, applied = _try_apply_lead(working, item)
            if applied:
                summary["leads_added"] += 1

        for item in result.get("lead_updates") or []:
            working, applied = _try_apply_lead_update(working, item)
            if applied:
                summary["lead_updates"] += 1

        _audit_state.clear()
        _audit_state.update(working)
        _persist()

    return summary


# --------------------------------------------------------------------------- #
# Lock-safe snapshot (R3 — never raw-iterate the module dicts)
# --------------------------------------------------------------------------- #


def _build_snapshot(caido_client: Any | None) -> dict[str, Any]:
    """Assemble a blackboard snapshot via the lock-protected accessors only.

    Reads are done through ``_get_audit_state_impl`` / ``qa_audit_summary``
    (taken under the store lock) and the other stores' ``qa_*_summary`` helpers,
    never by raw-iterating the module dicts (R3). Only ids/enums leave the
    stores; no raw values. A traffic digest is included only if a Caido client
    is present.
    """
    audit_doc = _get_audit_state_impl()
    audit_summary = qa_audit_summary()

    leads = [
        {
            "lead_id": entry.get("lead_id"),
            "priority": entry.get("priority"),
            "status": entry.get("status"),
        }
        for entry in list(audit_doc.get("leads", {}).values())[:_MAX_SNAPSHOT_LEADS]
        if isinstance(entry, dict)
    ]

    loot_refs: list[dict[str, Any]] = []
    notes_titles: list[str] = []
    try:
        loot_refs = list(qa_loot_summary().get("refs", []))[:_MAX_SNAPSHOT_LOOT]
    except Exception:  # noqa: BLE001 - a degraded store must not break reflection
        logger.debug("reflection: loot summary unavailable", exc_info=True)
    try:
        notes_titles = list(qa_notes_summary().get("signals", []))[:_MAX_SNAPSHOT_NOTES]
    except Exception:  # noqa: BLE001
        logger.debug("reflection: notes summary unavailable", exc_info=True)

    target_profile: dict[str, Any] = {}
    try:
        profile = _get_target_profile_impl()
        if isinstance(profile, dict):
            target_profile = {
                "target_types": profile.get("target_types"),
                "tech_stack": profile.get("tech_stack"),
            }
    except Exception:  # noqa: BLE001
        logger.debug("reflection: target profile unavailable", exc_info=True)

    qa_gaps: list[dict[str, Any]] = []
    try:
        review_context = {
            "target_types": set(target_profile.get("target_types") or []),
            "tool_history": [],
            "tool_history_available": False,
            "tool_history_partial": False,
            "proxy_sitemap_available": False,
            "signal_text": list(audit_summary.get("signals", [])),
            "_note_refs": [],
            "_loot_refs": loot_refs,
            "_audit_leads": audit_summary.get("refs", []),
            "_audit_lead_texts": {},
        }
        qa_gaps = [
            {"gap_id": g.get("gap_id"), "priority": g.get("priority"), "area": g.get("area")}
            for g in evaluate_qa_gaps(review_context)[:_MAX_SNAPSHOT_GAPS]
        ]
    except Exception:  # noqa: BLE001
        logger.debug("reflection: QA gap evaluation unavailable", exc_info=True)

    snapshot: dict[str, Any] = {
        "thesis": audit_doc.get("thesis", ""),
        "assumptions": audit_summary.get("refs", []),
        "leads": leads,
        "loot_refs": loot_refs,
        "notes_titles": notes_titles,
        "target_profile": target_profile,
        "qa_gaps": qa_gaps,
    }

    if caido_client is not None:
        snapshot["traffic_digest"] = _traffic_digest(caido_client)

    return snapshot


def _traffic_digest(caido_client: Any | None) -> dict[str, Any]:
    """Best-effort, never-raising traffic digest (present only with a client)."""
    if caido_client is None:
        return {}
    return {"available": True}


# --------------------------------------------------------------------------- #
# Impure runner — one structured model call, mirroring report/dedupe.py
# --------------------------------------------------------------------------- #


def _parse_reflection_response(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in reflection response")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Reflection response is not a JSON object")  # noqa: TRY004
    return parsed


async def _call_model(
    model_obj: Any, messages: list[dict[str, str]], settings: Any
) -> ModelResponse:
    user_msg = messages[-1]["content"] if messages else ""
    response = await model_obj.get_response(
        system_instructions=REFLECTION_PROMPT,
        input=user_msg,
        model_settings=ModelSettings(
            retry=model_retry_settings_from_config(settings),
            include_usage=True,
        ),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )
    return cast("ModelResponse", response)


async def run_reflection(
    *,
    model: str,
    caido_client: Any | None = None,
    coordinator: Any | None = None,
) -> dict[str, Any]:
    """Snapshot the blackboard (lock-safe), call the model once (structured),
    parse tolerantly (one retry, else clean skip), apply, and record usage.

    Never raises into the caller. Skips entirely when ``coordinator`` reports a
    budget stop (checked here in addition to the hook-level gate).
    """
    result: dict[str, Any] = {"applied": False, "skipped_reason": None}
    try:
        if coordinator is not None and getattr(coordinator, "budget_stopped", False):
            result["skipped_reason"] = "budget_stopped"
            return result

        snapshot = _build_snapshot(caido_client)
        messages = build_reflection_input(snapshot)

        settings = load_settings()
        configure_sdk_model_defaults(settings)
        resolved_model = (model or "").strip()
        model_obj = StrixProvider().get_model(resolved_model)

        parsed: dict[str, Any] | None = None
        for attempt in range(2):  # one call + one retry
            try:
                response = await _call_model(model_obj, messages, settings)
            except Exception:  # a model failure must not crash teardown
                logger.exception("reflection model call failed (attempt %d)", attempt + 1)
                continue

            report_state = get_global_report_state()
            if report_state is not None:
                try:
                    report_state.record_sdk_usage(
                        agent_id="reflection",
                        agent_name="reflection",
                        model=resolved_model,
                        usage=response.usage,
                    )
                except Exception:
                    logger.exception("reflection: failed to record usage")

            content = _extract_text(response)
            try:
                parsed = _parse_reflection_response(content)
                break
            except (ValueError, json.JSONDecodeError):
                logger.warning(
                    "reflection: unparseable model output (attempt %d)", attempt + 1
                )
                parsed = None

        if parsed is None:
            result["skipped_reason"] = "unparseable"
            return result

        summary = apply_reflection(parsed)
        result["applied"] = True
        result["summary"] = summary
    except Exception:  # reflection must NEVER raise into the caller
        logger.exception("run_reflection failed")
        result["skipped_reason"] = "error"
    return result


# --------------------------------------------------------------------------- #
# Single-flight scheduler (module-level; coalesces bursts to <=1 extra run)
# --------------------------------------------------------------------------- #


_reflection_lock: asyncio.Lock = asyncio.Lock()
_reflection_dirty: bool = False
_reflection_running: bool = False


def _schedule_reflection(
    *,
    model: str,
    caido_client: Any | None,
    coordinator: Any | None = None,
) -> None:
    """Schedule a reflection as a background task with single-flight coalescing.

    If a reflection is already running, set the dirty flag and return; when the
    running one finishes it re-runs once if dirty. This collapses a burst of
    near-simultaneous child completions into at most one extra reflection.
    Exceptions from the scheduled task are logged in a done-callback so they
    never propagate or become unretrieved-task errors.

    The skip decision is driven by the synchronous ``_reflection_running`` bool
    (compare-and-swap with no ``await`` in between) so that back-to-back calls
    issued within a single event-loop tick cannot both spawn a runner — the
    ``asyncio.Lock`` alone would not prevent that, since it is only acquired
    inside the scheduled coroutine on a later tick.
    """
    global _reflection_dirty, _reflection_running  # noqa: PLW0603

    # Synchronous compare-and-swap: no await between the check and the set.
    if _reflection_running:
        _reflection_dirty = True
        return
    _reflection_running = True

    async def _runner() -> None:
        global _reflection_dirty, _reflection_running  # noqa: PLW0603
        try:
            async with _reflection_lock:
                await run_reflection(
                    model=model, caido_client=caido_client, coordinator=coordinator
                )
        finally:
            _reflection_running = False
            if _reflection_dirty:
                _reflection_dirty = False
                _schedule_reflection(
                    model=model, caido_client=caido_client, coordinator=coordinator
                )

    def _done(task: asyncio.Task[None]) -> None:
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.error("scheduled reflection task failed", exc_info=exc)

    try:
        task = asyncio.create_task(_runner())
    except RuntimeError:
        # No running event loop (e.g. called from a sync context) — skip cleanly.
        # The runner never ran, so release the single-flight flag we just set.
        _reflection_running = False
        logger.debug("reflection: no running loop; skipping schedule")
        return
    task.add_done_callback(_done)

"""In-place session compaction — bounded, blackboard-anchored agent memory.

The SDK stores each agent's full transcript and replays it every turn; left to
grow it eventually exceeds the model's context window (see
``strix.core.context_limit``). ``ContextLimitFilter`` only trims the *outbound*
copy, so the persisted session grows without bound and a resume reloads the full
history. This module bounds the *stored* session: when an agent's estimated fill
crosses a threshold, the oldest turns are summarised into one compact memory item
— a **pointer index** into the durable state stores plus a short narrative digest
— while the task (first item) and the most recent turns are kept verbatim. The
rewrite is persisted, so growth is bounded and a resume reloads the compacted
history.

Design notes:
* Retrieval is **by index, not grep** — the memory item carries store IDs
  (note/loot/vuln/lead) so the model fetches full records on demand via
  ``get_note`` / ``get_loot`` / ``get_audit_state``. No verbatim bulk is kept.
* The summary model call mirrors ``strix.core.reflection`` / ``report.dedupe`` —
  one ``get_response`` outside the run's hook/session path, never raising.
* Compaction reuses ``context_limit._repair_pairs`` so a ``function_call`` and
  its ``function_call_output`` are never split across the compaction boundary.
* ``compact_session`` mirrors the ``get_items → clear_session → add_items``
  mutation pattern already used by ``sessions.strip_all_images_from_session``.
"""

from __future__ import annotations

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
from strix.core.context_limit import (
    _APPROX_BYTES_PER_TOKEN,
    _est_tokens,
    _repair_pairs,
)
from strix.report.dedupe import _extract_text


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agents.items import TResponseInputItem
    from agents.memory import Session

    from strix.core.context_limit import ContextLimitFilter

logger = logging.getLogger(__name__)

_COMPACTION_MARKER = (
    "[Strix compacted older turns to fit the context window. The durable detail "
    "persists in the state stores — fetch full records by the IDs below with "
    "get_note / get_loot / get_audit_state / list_todos. A pointer index and a "
    "short digest of the compacted turns follow.]\n\n"
)

_SUMMARIZER_PROMPT = (
    "You are the compaction step of an autonomous security assessment. You are "
    "given a pointer index of the durable state (findings, notes, loot, leads, "
    "todos) followed by the raw older turns of one agent's transcript that are "
    "about to be dropped to save context. Produce a TERSE factual digest that "
    "preserves what the agent must not forget: decisions taken, actions tried "
    "and their results INCLUDING negative results ('tested X, clean'), and open "
    "threads still worth pursuing. Reference store IDs from the index rather than "
    "restating their contents. Do NOT invent anything not present in the input. "
    "No preamble, no markdown headers — just the digest."
)

# Cap on how much of the old span is fed to the summariser, so the summarisation
# call itself stays well within budget even when the span is huge. The dropped
# remainder is still recoverable from the durable stores by ID.
_MAX_SUMMARY_INPUT_CHARS = 48_000
_MAX_INDEX_CHARS = 6_000
# Compact to comfortably below the trigger so a single compaction lasts many
# turns instead of re-firing every turn.
_COMPACTION_TARGET_FACTOR = 0.7


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " …[clipped]"


# --------------------------------------------------------------------------- #
# Blackboard pointer index — one small, defensive reader per store.
# Local imports: the tool packages pull in heavy deps and could form an import
# cycle at module load; the index is only rendered at compaction time.
# --------------------------------------------------------------------------- #


def _section_audit_state() -> str | None:
    try:
        from strix.tools.audit_state.tools import _snapshot_audit_state  # noqa: PLC0415

        doc = _snapshot_audit_state()
        lines: list[str] = []
        thesis = str(doc.get("thesis") or "").strip()
        if thesis:
            lines.append(f"thesis: {_clip(thesis, 600)}")
        assumptions = doc.get("assumptions") or {}
        if isinstance(assumptions, dict) and assumptions:
            parts = [
                f"{k}(conf={(v or {}).get('confidence', '?')})"
                for k, v in list(assumptions.items())[:20]
                if isinstance(v, dict)
            ]
            if parts:
                lines.append("assumptions: " + "; ".join(parts))
        leads = doc.get("leads") or {}
        if isinstance(leads, dict) and leads:
            parts = [
                f"{lid}[{(v or {}).get('status', '?')}/{(v or {}).get('priority', '?')}]"
                for lid, v in list(leads.items())[:20]
                if isinstance(v, dict)
            ]
            if parts:
                lines.append("leads: " + "; ".join(parts))
        return "== AUDIT STATE ==\n" + "\n".join(lines) if lines else None
    except Exception:  # noqa: BLE001 - a degraded store must never break the index
        logger.debug("blackboard index: audit_state unavailable", exc_info=True)
        return None


def _section_findings() -> str | None:
    try:
        from strix.report.state import get_global_report_state  # noqa: PLC0415

        state = get_global_report_state()
        if state is None:
            return None
        parts = [
            f"{v.get('id', '?')}[{v.get('severity', '?')}] {_clip(str(v.get('title', '')), 80)}"
            for v in state.get_existing_vulnerabilities()[:40]
            if isinstance(v, dict)
        ]
        return "== FINDINGS (filed) ==\n" + " ; ".join(parts) if parts else None
    except Exception:  # noqa: BLE001 - a degraded store must never break the index
        logger.debug("blackboard index: report state unavailable", exc_info=True)
        return None


def _section_notes() -> str | None:
    try:
        from strix.tools.notes.tools import qa_notes_summary  # noqa: PLC0415

        parts = [
            f"{r.get('note_id', '?')}[{r.get('category', 'general')}]"
            + (f" {','.join(r.get('tags', [])[:4])}" if r.get("tags") else "")
            for r in qa_notes_summary().get("refs", [])[:40]
            if isinstance(r, dict)
        ]
        return "== NOTES (get_note) ==\n" + " ; ".join(parts) if parts else None
    except Exception:  # noqa: BLE001 - a degraded store must never break the index
        logger.debug("blackboard index: notes unavailable", exc_info=True)
        return None


def _section_loot() -> str | None:
    try:
        from strix.tools.loot.tools import qa_loot_summary  # noqa: PLC0415

        parts = [
            f"{r.get('loot_id', '?')}[{r.get('loot_type', 'other')}"
            + (f"/{r.get('scope')}" if r.get("scope") else "")
            + "]"
            for r in qa_loot_summary().get("refs", [])[:40]
            if isinstance(r, dict)
        ]
        return "== LOOT (get_loot) ==\n" + " ; ".join(parts) if parts else None
    except Exception:  # noqa: BLE001 - a degraded store must never break the index
        logger.debug("blackboard index: loot unavailable", exc_info=True)
        return None


def _section_todos() -> str | None:
    try:
        from strix.tools.todo.tools import unresolved_todos_summary  # noqa: PLC0415

        parts = [
            f"{td.get('todo_id', '?')}[{td.get('status', '?')}/"
            f"{td.get('priority', '?')}] {_clip(str(td.get('title', '')), 60)}"
            for entry in unresolved_todos_summary()[:20]
            for td in entry.get("todos", [])[:5]
        ]
        return "== OPEN TODOS ==\n" + " ; ".join(parts[:40]) if parts else None
    except Exception:  # noqa: BLE001 - a degraded store must never break the index
        logger.debug("blackboard index: todos unavailable", exc_info=True)
        return None


_INDEX_SECTIONS = (
    _section_audit_state,
    _section_findings,
    _section_notes,
    _section_loot,
    _section_todos,
)


def render_blackboard_index() -> str:
    """Assemble a compact, bounded pointer index from the durable state stores.

    Pure reads (never raises). Values are IDs + minimal labels only — full
    records are fetched on demand by ID. Bounded regardless of run length.
    """
    sections = [section for fn in _INDEX_SECTIONS if (section := fn()) is not None]
    return _clip("\n\n".join(sections), _MAX_INDEX_CHARS)


# --------------------------------------------------------------------------- #
# Summarisation + compaction
# --------------------------------------------------------------------------- #


def _serialize_for_summary(items: list[Any]) -> str:
    chunks: list[str] = []
    for item in items:
        try:
            chunks.append(json.dumps(item, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            chunks.append(str(item))
    return _clip("\n".join(chunks), _MAX_SUMMARY_INPUT_CHARS)


async def summarize_span(
    old_items: list[Any],
    blackboard_index: str,
    *,
    model: str,
    settings: Any = None,
) -> str:
    """One structured model call to digest the compacted span. Never raises.

    Mirrors ``reflection._call_model`` (no raw litellm, SDK provider, tracing
    disabled). Returns ``""`` on any failure so compaction still proceeds with an
    index-only memory item.
    """
    if not (model or "").strip() or not old_items:
        return ""
    try:
        settings = settings or load_settings()
        configure_sdk_model_defaults(settings)
        model_obj = StrixProvider().get_model(model.strip())
        user_msg = (
            f"{blackboard_index}\n\n== RAW OLDER TURNS TO COMPACT ==\n"
            f"{_serialize_for_summary(old_items)}"
        )
        response = await model_obj.get_response(
            system_instructions=_SUMMARIZER_PROMPT,
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
        return _extract_text(response).strip()
    except Exception:
        logger.exception("summarize_span model call failed; using index-only memory")
        return ""


async def compact_session(
    session: Session,
    *,
    keep_recent: int,
    target_tokens: int,
    summarize: Callable[[list[Any], str], Awaitable[str]],
    blackboard_index: str = "",
    bytes_per_token: float = _APPROX_BYTES_PER_TOKEN,
) -> bool:
    """Rewrite ``session`` in place: pin the task, summarise the oldest span into
    one index+digest memory item, keep the most recent items verbatim.

    Returns ``True`` if the stored session was rewritten. Returns ``False`` when
    already small enough, or when the task + recency window alone exceed the
    target (the escalation signal — the caller should shrink or hand off).
    """
    items = list(await session.get_items())
    if not items:
        return False
    if _est_tokens(items, bytes_per_token) <= target_tokens:
        return False

    pinned = items[:1]
    tail_start = max(1, len(items) - keep_recent) if keep_recent > 0 else len(items)
    recent = items[tail_start:]
    old = items[1:tail_start]
    if not old:
        # Task + recency window already dominate; compaction can't help here.
        return False

    digest = await summarize(old, blackboard_index)
    content = _COMPACTION_MARKER + blackboard_index
    if digest:
        content += "\n\n== DIGEST OF COMPACTED TURNS ==\n" + digest
    memory_item: dict[str, Any] = {"role": "user", "content": content}

    rebuilt = _repair_pairs([*pinned, memory_item, *recent])
    await session.clear_session()
    await session.add_items(cast("list[TResponseInputItem]", rebuilt))
    return True


def _compaction_plan(
    context_filter: ContextLimitFilter,
    items: list[Any],
    coordinator: Any,
) -> tuple[int, int, str] | None:
    """Decide whether to compact and with what parameters.

    Returns ``(keep_recent, target_tokens, model)`` or ``None`` when compaction
    is disabled, budget-stopped, or the fill is still under the trigger.
    """
    ratio = getattr(context_filter, "compaction_trigger_ratio", 0.0)
    if ratio <= 0:
        return None
    if coordinator is not None and getattr(coordinator, "budget_stopped", False):
        return None
    window = context_filter.effective_window()
    if window <= 0 or context_filter.estimate_tokens(items) < int(window * ratio):
        return None
    keep_recent = getattr(context_filter, "compaction_keep_recent", 0)
    target = int(window * ratio * _COMPACTION_TARGET_FACTOR)
    model = getattr(context_filter, "summarizer_model", "") or ""
    return keep_recent, target, model


async def maybe_compact_session(
    session: Session | None,
    *,
    context_filter: ContextLimitFilter | None,
    coordinator: Any = None,
) -> bool:
    """Compact ``session`` if its estimated fill has crossed the trigger ratio.

    Shared entry point for both the proactive (``on_llm_end``) and reactive
    (``_run_cycle`` recovery) callers. Reads all tuning + the live window/estimate
    off the shared ``context_filter``. Budget-aware and never raises.
    """
    if session is None or context_filter is None:
        return False
    try:
        items = list(await session.get_items())
    except Exception:
        logger.exception("maybe_compact_session: get_items failed")
        return False

    plan = _compaction_plan(context_filter, items, coordinator)
    if plan is None:
        return False
    keep_recent, target, model = plan
    index = render_blackboard_index()

    async def _summ(old: list[Any], idx: str) -> str:
        return await summarize_span(old, idx, model=model)

    try:
        return await compact_session(
            session,
            keep_recent=keep_recent,
            target_tokens=target,
            summarize=_summ,
            blackboard_index=index,
            bytes_per_token=context_filter.bytes_per_token,
        )
    except Exception:
        logger.exception("maybe_compact_session: compaction failed")
        return False

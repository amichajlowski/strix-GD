"""Proactive + adaptive model-context size limiting.

The SDK stores each agent's full conversation history and replays it on every
turn. Left unbounded it eventually exceeds the model's context window and the
endpoint hard-rejects the request with a 400, killing the agent (and, in a
non-interactive scan, that whole branch of work).

``ContextLimitFilter`` is wired into the single shared ``RunConfig`` as its
``call_model_input_filter`` — the SDK invokes it immediately before every model
call, so one instance covers the root and all child agents. It trims the
*outbound* payload only; the persisted session on disk is never mutated, so
nothing is lost permanently and each turn re-trims from full history.

It also carries a learned window: when a context-length 400 slips through
anyway (window configured too high, or the estimate undercounted), the run loop
parses the true limit from the rejection via :func:`parse_context_length_from_error`
and calls :meth:`ContextLimitFilter.note_context_length`, lowering the live
budget so the retry fits. That makes the cap self-correcting for local models
whose real context window Strix cannot know in advance.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import TYPE_CHECKING, Any

from agents.run_config import ModelInputData


if TYPE_CHECKING:
    from agents.run_config import CallModelData

logger = logging.getLogger(__name__)

# Rough tokens-per-byte estimate, matching the SDK sandbox convention
# (agents.sandbox.util.token_truncation.APPROX_BYTES_PER_TOKEN), but biased
# conservative (more tokens per byte than the SDK's 4) so the estimate errs
# toward over-counting for token-dense content (JSON, JWTs, base64 loot).
# ponytail: bytes/N estimate, swap for a real tokenizer only if it proves too coarse.
_APPROX_BYTES_PER_TOKEN = 3.5

# Reserve is the larger of this flat floor and a percentage of the window (see
# _RESERVE_RATIO), kept below the raw window for the model's own output plus
# slack in the bytes/N estimate.
_RESERVE_TOKENS = 16384
_RESERVE_RATIO = 0.10

# shrink() multiplies this factor; once it would fall below the floor, further
# shrinking is refused and the caller must escalate (compaction/successor).
_SHRINK_FLOOR = 0.25

_TRUNCATION_MARKER = "[older context truncated by Strix to fit the model context window]"
_ITEM_TRUNCATED_MARKER = "\n\n[...truncated by Strix to fit the model context window...]"

_CONTEXT_LEN_RE = re.compile(r"maximum context length is\s+(\d+)\s+tokens", re.IGNORECASE)


def parse_context_length_from_error(message: str | None) -> int | None:
    """Extract the model's true max context length from a provider 400 message.

    Returns the integer token cap the provider reported, or ``None`` if the
    message is not a recognisable context-length rejection.
    """
    if not message:
        return None
    match = _CONTEXT_LEN_RE.search(message)
    return int(match.group(1)) if match else None


def _est_tokens(items: list[Any], bytes_per_token: float = _APPROX_BYTES_PER_TOKEN) -> int:
    total_bytes = 0
    for item in items:
        try:
            total_bytes += len(json.dumps(item, ensure_ascii=False, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            total_bytes += len(str(item).encode("utf-8"))
    return int(total_bytes / bytes_per_token)


def _est_tokens_text(text: str, bytes_per_token: float = _APPROX_BYTES_PER_TOKEN) -> int:
    return int(len(text.encode("utf-8")) / bytes_per_token)


def _repair_pairs(items: list[Any]) -> list[Any]:
    """Drop tool-call / tool-output items whose partner was trimmed away.

    The chat-completions route (used by local/LiteLLM models) 400s on a
    ``function_call_output`` with no preceding ``function_call``, and can reject a
    dangling ``function_call``. Keep a call/output only when both survive.
    """
    call_ids = {
        item.get("call_id")
        for item in items
        if isinstance(item, dict) and item.get("type") == "function_call" and item.get("call_id")
    }
    output_ids = {
        item.get("call_id")
        for item in items
        if isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and item.get("call_id")
    }
    paired = call_ids & output_ids

    kept: list[Any] = []
    for item in items:
        if (
            isinstance(item, dict)
            and item.get("type") in ("function_call", "function_call_output")
            and item.get("call_id") not in paired
        ):
            continue
        kept.append(item)
    return kept


def _clip_text(value: str, keep_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= keep_bytes:
        return value
    clipped = encoded[:keep_bytes].decode("utf-8", errors="ignore")
    return clipped + _ITEM_TRUNCATED_MARKER


def _truncate_item_text(item: Any) -> Any:
    """Halve the largest text payload inside a single oversized item."""
    if not isinstance(item, dict):
        text = str(item)
        return _clip_text(text, len(text.encode("utf-8")) // 2)

    new_item = dict(item)
    content = new_item.get("content")
    if isinstance(content, str) and content:
        new_item["content"] = _clip_text(content, len(content.encode("utf-8")) // 2)
        return new_item
    output = new_item.get("output")
    if isinstance(output, str) and output:
        new_item["output"] = _clip_text(output, len(output.encode("utf-8")) // 2)
        return new_item
    # Structured content/output blocks: clip the first text-bearing block.
    for key in ("content", "output"):
        blocks = new_item.get(key)
        if isinstance(blocks, list):
            rebuilt = []
            clipped_one = False
            for block in blocks:
                if (
                    not clipped_one
                    and isinstance(block, dict)
                    and isinstance(block.get("text"), str)
                    and block["text"]
                ):
                    text = block["text"]
                    rebuilt.append(
                        {**block, "text": _clip_text(text, len(text.encode("utf-8")) // 2)}
                    )
                    clipped_one = True
                else:
                    rebuilt.append(block)
            if clipped_one:
                new_item[key] = rebuilt
                return new_item
    return new_item


def _largest_item_index(
    items: list[Any], bytes_per_token: float = _APPROX_BYTES_PER_TOKEN
) -> int | None:
    best_index: int | None = None
    best_size = -1
    for index, item in enumerate(items):
        size = _est_tokens([item], bytes_per_token)
        if size > best_size:
            best_size = size
            best_index = index
    return best_index


def _enforce_hard_cap(
    items: list[Any], budget: int, bytes_per_token: float = _APPROX_BYTES_PER_TOKEN
) -> list[Any]:
    """Last resort when a single kept item alone exceeds budget: clip its text.

    Prevents ever emitting an over-budget payload (which the provider would 400
    on) even in the degenerate case of one enormous tool output. Bounded loop —
    halving converges fast.
    """
    guard = 0
    while _est_tokens(items, bytes_per_token) > budget and guard < len(items) + 8:
        index = _largest_item_index(items, bytes_per_token)
        if index is None:
            break
        items = [*items[:index], _truncate_item_text(items[index]), *items[index + 1 :]]
        guard += 1
    return items


def trim_items(
    items: list[Any], budget: int, bytes_per_token: float = _APPROX_BYTES_PER_TOKEN
) -> tuple[list[Any], bool]:
    """Trim ``items`` to fit ``budget`` tokens. Returns ``(items, changed)``.

    Strategy: pin the first item (the agent's task), insert a truncation marker,
    then keep as many of the most-recent items as fit. Old middle turns are
    dropped. Tool-call pairing is repaired so no orphaned call/output survives.
    """
    if budget <= 0 or not items:
        return items, False
    if _est_tokens(items, bytes_per_token) <= budget:
        return items, False

    head = items[:1]
    marker: list[Any] = [{"role": "user", "content": _TRUNCATION_MARKER}]
    fixed = _est_tokens(head, bytes_per_token) + _est_tokens(marker, bytes_per_token)

    tail: list[Any] = []
    for item in reversed(items[1:]):
        item_tokens = _est_tokens(tail, bytes_per_token) + _est_tokens([item], bytes_per_token)
        if fixed + item_tokens > budget:
            break
        tail.insert(0, item)

    kept = _repair_pairs([*head, *marker, *tail])
    kept = _enforce_hard_cap(kept, budget, bytes_per_token)
    return kept, True


class ContextLimitFilter:
    """Callable ``call_model_input_filter`` that caps outbound context size.

    Holds a mutable learned window so the run loop can lower the effective budget
    after a context-length 400 (see module docstring). The instance is shared via
    the single ``RunConfig`` across every agent.
    """

    def __init__(
        self,
        configured_window: int,
        *,
        reserve_ratio: float = _RESERVE_RATIO,
        bytes_per_token: float = _APPROX_BYTES_PER_TOKEN,
        summarizer_model: str | None = None,
        compaction_keep_recent: int = 0,
        compaction_trigger_ratio: float = 0.0,
    ) -> None:
        self._configured_window = configured_window
        self._learned_window: int | None = None
        self._reserve_ratio = reserve_ratio
        self._bytes_per_token = bytes_per_token
        self._shrink_factor = 1.0
        # Carried here (not imported) so the compaction module can read config +
        # the live window/estimate off the single shared filter without a cycle:
        # compaction imports context_limit, never the reverse.
        self.summarizer_model = summarizer_model
        self.compaction_keep_recent = compaction_keep_recent
        self.compaction_trigger_ratio = compaction_trigger_ratio

    @property
    def bytes_per_token(self) -> float:
        return self._bytes_per_token

    def effective_window(self) -> int:
        """The live window (configured, lowered by any learned provider max)."""
        return self._window()

    def estimate_tokens(self, items: list[Any]) -> int:
        """Token estimate for ``items`` using this filter's calibrated divisor."""
        return _est_tokens(items, self._bytes_per_token)

    def reset_shrink(self) -> None:
        """Clear the transient shrink applied during 400-recovery.

        Called after a successful cycle so a one-off overflow does not
        permanently penalise this (shared) filter's budget for the rest of the
        run. Compaction is the durable reducer; shrink is only a last resort.
        """
        self._shrink_factor = 1.0

    def note_context_length(self, reported_max: int) -> bool:
        """Record the provider's true context limit. Returns True if it lowered
        the effective budget (i.e. a retry is worthwhile)."""
        if reported_max <= 0:
            return False
        if self._learned_window is not None and reported_max >= self._learned_window:
            return False
        self._learned_window = reported_max
        logger.info("Context-limit filter learned provider max context = %d tokens", reported_max)
        return True

    def shrink(self, factor: float) -> bool:
        """Multiply the effective budget by ``factor``.

        Unlike :meth:`note_context_length`, this shrinks the budget even when the
        provider keeps reporting the same max — the recovery path for a model
        whose real limit is already known but whose token estimate was still too
        optimistic. Returns ``False`` once a floor is hit so the caller can
        escalate (compaction/successor) instead of shrinking forever.
        """
        candidate = self._shrink_factor * factor
        if candidate < _SHRINK_FLOOR:
            return False
        self._shrink_factor = candidate
        return True

    def _window(self) -> int:
        window = self._configured_window
        if self._learned_window is not None:
            window = min(window, self._learned_window)
        return window

    def _budget(self, instruction_tokens: int = 0) -> int:
        window = self._window()
        reserve = max(_RESERVE_TOKENS, math.ceil(window * self._reserve_ratio))
        # Never let reserve + instructions drive the budget below half the window.
        base = max(window // 2, window - reserve - instruction_tokens)
        return int(base * self._shrink_factor)

    def __call__(self, data: CallModelData[Any]) -> ModelInputData:
        model_data = data.model_data
        instruction_tokens = _est_tokens_text(model_data.instructions or "", self._bytes_per_token)
        budget = self._budget(instruction_tokens)
        trimmed, changed = trim_items(list(model_data.input), budget, self._bytes_per_token)
        if not changed:
            return model_data
        logger.info(
            "Context-limit filter trimmed request from %d to %d items (budget=%d tokens)",
            len(model_data.input),
            len(trimmed),
            budget,
        )
        return ModelInputData(input=trimmed, instructions=model_data.instructions)

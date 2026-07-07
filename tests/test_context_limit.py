"""Tests for proactive + adaptive model-context size limiting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from strix.core.context_limit import (
    ContextLimitFilter,
    _est_tokens,
    parse_context_length_from_error,
    trim_items,
)


@dataclass
class _ModelData:
    input: list[Any]
    instructions: str | None = None


@dataclass
class _CallData:
    model_data: _ModelData
    agent: Any = None
    context: Any = None


def _big_text(approx_tokens: int) -> str:
    # bytes/4 estimate -> this many tokens
    return "x" * (approx_tokens * 4)


def test_parse_context_length_from_error() -> None:
    msg = (
        "Error code: 400 - This model's maximum context length is 262144 tokens. "
        "However, you requested 0 output tokens and your prompt contains 262145..."
    )
    assert parse_context_length_from_error(msg) == 262144
    assert parse_context_length_from_error("some other 400") is None
    assert parse_context_length_from_error(None) is None


def test_trim_noop_when_under_budget() -> None:
    items = [{"role": "user", "content": "task"}, {"role": "assistant", "content": "ok"}]
    trimmed, changed = trim_items(items, budget=100_000)
    assert changed is False
    assert trimmed == items


def test_trim_disabled_when_budget_zero() -> None:
    items = [{"role": "user", "content": _big_text(50_000)}]
    trimmed, changed = trim_items(items, budget=0)
    assert changed is False
    assert trimmed == items


def test_trim_pins_task_and_keeps_recent() -> None:
    task = {"role": "user", "content": "TASK: audit the target"}
    filler = [{"role": "assistant", "content": _big_text(2_000)} for _ in range(20)]
    recent = {"role": "assistant", "content": "MOST RECENT"}
    items = [task, *filler, recent]

    trimmed, changed = trim_items(items, budget=5_000)

    assert changed is True
    assert trimmed[0] == task  # task pinned
    assert trimmed[1]["content"].startswith("[older context truncated")
    assert trimmed[-1] == recent  # newest kept
    assert len(trimmed) < len(items)


def test_trim_repairs_orphaned_tool_output() -> None:
    # The function_call is old and gets dropped; its output must not survive alone.
    task = {"role": "user", "content": "task"}
    old_call = {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"}
    bloat = {"role": "assistant", "content": _big_text(10_000)}
    orphan_output = {"type": "function_call_output", "call_id": "c1", "output": "result"}
    items = [task, old_call, bloat, orphan_output]

    trimmed, _ = trim_items(items, budget=3_000)

    call_ids = {i.get("call_id") for i in trimmed if isinstance(i, dict)}
    types = {i.get("type") for i in trimmed if isinstance(i, dict)}
    # If the paired call was dropped, its output must be dropped too.
    if "c1" not in {i.get("call_id") for i in trimmed if i.get("type") == "function_call"}:
        assert "function_call_output" not in types or "c1" not in call_ids


def test_trim_paired_call_and_output_survive_together() -> None:
    task = {"role": "user", "content": "task"}
    call = {"type": "function_call", "call_id": "c9", "name": "shell", "arguments": "{}"}
    output = {"type": "function_call_output", "call_id": "c9", "output": "recent result"}
    items = [task, call, output]

    trimmed, changed = trim_items(items, budget=100_000)
    assert changed is False
    assert call in trimmed and output in trimmed


def test_hard_cap_clips_single_oversized_item() -> None:
    # One enormous tool output, larger than the whole budget on its own.
    task = {"role": "user", "content": "task"}
    huge = {"type": "function_call_output", "call_id": "c1", "output": _big_text(50_000)}
    call = {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"}
    items = [task, call, huge]

    trimmed, changed = trim_items(items, budget=2_000)
    assert changed is True
    # Never emit an over-budget payload, even in the degenerate case.
    assert _est_tokens(trimmed) <= 2_000


def test_filter_learns_lower_window() -> None:
    flt = ContextLimitFilter(configured_window=262_144)

    # Under the big configured budget -> no trim.
    small = _CallData(_ModelData(input=[{"role": "user", "content": "hi"}]))
    assert flt(small).input == small.model_data.input

    # Learn a much smaller real window (e.g. a 32K local model).
    assert flt.note_context_length(32_768) is True
    # Learning the same-or-larger value again does nothing.
    assert flt.note_context_length(32_768) is False
    assert flt.note_context_length(100_000) is False

    # Now a payload that fit under 256K but not under 32K gets trimmed.
    task = {"role": "user", "content": "task"}
    filler = [{"role": "assistant", "content": _big_text(3_000)} for _ in range(20)]
    big = _CallData(_ModelData(input=[task, *filler], instructions="sys"))
    result = flt(big)
    assert len(result.input) < len(big.model_data.input)
    assert result.instructions == "sys"  # instructions untouched


def test_filter_noop_returns_same_object() -> None:
    flt = ContextLimitFilter(configured_window=262_144)
    data = _CallData(_ModelData(input=[{"role": "user", "content": "hi"}], instructions="s"))
    # No trim needed -> hand back the original model_data unchanged.
    assert flt(data) is data.model_data


# --- T1: honest meter + margin -----------------------------------------------


def test_budget_reserves_instruction_tokens() -> None:
    window = 200_000
    task = {"role": "user", "content": "TASK: audit"}
    filler = [{"role": "assistant", "content": _big_text(5_000)} for _ in range(30)]
    recent = {"role": "assistant", "content": "MOST RECENT"}
    items = [task, *filler, recent]
    instructions = _big_text(60_000)

    no_instr = ContextLimitFilter(configured_window=window, bytes_per_token=4.0)
    result_no_instr = no_instr(_CallData(_ModelData(input=items, instructions=None)))

    with_instr = ContextLimitFilter(configured_window=window, bytes_per_token=4.0)
    result_with_instr = with_instr(_CallData(_ModelData(input=items, instructions=instructions)))

    # Same history fits without the system prompt but must be trimmed harder
    # once its token cost is reserved out of the budget.
    assert len(result_with_instr.input) < len(result_no_instr.input)


def test_reserve_is_percentage_of_window() -> None:
    small = ContextLimitFilter(configured_window=50_000)
    large = ContextLimitFilter(configured_window=300_000)
    # 50_000 * 0.10 = 5_000 < the 16_384 floor -> floor wins.
    assert small._budget() == 50_000 - 16_384
    # 300_000 * 0.10 = 30_000 > the floor -> percentage wins.
    assert large._budget() == 300_000 - 30_000


def test_conservative_estimate_overcounts() -> None:
    text_item = [{"role": "user", "content": "x" * 4_000}]
    conservative = _est_tokens(text_item, bytes_per_token=3.5)
    old = _est_tokens(text_item, bytes_per_token=4.0)
    assert conservative >= old


def test_trims_to_fit_including_instructions() -> None:
    window = 200_000
    task = {"role": "user", "content": "TASK: audit"}
    filler = [{"role": "assistant", "content": _big_text(5_000)} for _ in range(34)]
    items = [task, *filler]

    no_instr = ContextLimitFilter(configured_window=window, bytes_per_token=4.0)
    result = no_instr(_CallData(_ModelData(input=items, instructions=None)))
    assert result.input == items  # fits when instructions aren't counted

    with_instr = ContextLimitFilter(configured_window=window, bytes_per_token=4.0)
    result2 = with_instr(_CallData(_ModelData(input=items, instructions=_big_text(60_000))))
    assert len(result2.input) < len(items)  # same history overflows once instructions count


# --- T2: progressive back-off recovery ---------------------------------------


def test_shrink_lowers_budget() -> None:
    flt = ContextLimitFilter(configured_window=200_000, bytes_per_token=4.0)
    before = flt._budget()
    assert flt.shrink(0.85) is True
    assert flt._budget() < before


def test_shrink_has_floor() -> None:
    flt = ContextLimitFilter(configured_window=200_000)
    shrinks = 0
    while flt.shrink(0.85):
        shrinks += 1
        assert shrinks < 100  # safety net against an infinite loop on a logic bug
    assert shrinks > 0
    # Floor reached: further shrink attempts keep returning False.
    assert flt.shrink(0.85) is False

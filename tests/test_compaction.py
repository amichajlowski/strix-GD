from __future__ import annotations

from typing import Any

import pytest

from strix.core.compaction import (
    compact_session,
    maybe_compact_session,
    render_blackboard_index,
)
from strix.core.context_limit import ContextLimitFilter


class FakeSession:
    """Minimal in-memory stand-in for the SDK SQLiteSession.

    Persists across get/clear/add like the real store, so a rewrite is
    observable on a subsequent read (models the resume-reload guarantee).
    """

    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)

    async def get_items(self) -> list[Any]:
        return list(self._items)

    async def clear_session(self) -> None:
        self._items = []

    async def add_items(self, items: list[Any]) -> None:
        self._items.extend(items)


def _msg(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": text}


def _call(call_id: str) -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": "shell", "arguments": "{}"}


def _output(call_id: str) -> dict[str, Any]:
    return {"type": "function_call_output", "call_id": call_id, "output": "ok"}


async def _stub_summary(_old: list[Any], _index: str) -> str:
    return "DIGEST"


# --- T4: compact_session ------------------------------------------------------


@pytest.mark.asyncio
async def test_compaction_pins_task_and_keeps_recent() -> None:
    task = _msg("user", "TASK: audit")
    middle = [_msg("assistant", f"old {i} " + "x" * 500) for i in range(20)]
    recent = [_msg("assistant", "RECENT-A"), _msg("assistant", "RECENT-B")]
    session = FakeSession([task, *middle, *recent])

    changed = await compact_session(
        session, keep_recent=2, target_tokens=100, summarize=_stub_summary
    )

    assert changed is True
    items = await session.get_items()
    assert items[0] == task  # task pinned verbatim
    assert items[-2:] == recent  # recent window verbatim
    assert len(items) < 2 + len(middle)  # middle was collapsed


@pytest.mark.asyncio
async def test_compaction_inserts_single_index_marker() -> None:
    session = FakeSession(
        [_msg("user", "TASK"), *[_msg("assistant", "x" * 500) for _ in range(10)]]
    )

    await compact_session(
        session,
        keep_recent=1,
        target_tokens=50,
        summarize=_stub_summary,
        blackboard_index="== NOTES (get_note) ==\nnote-1[auth]",
    )

    items = await session.get_items()
    markers = [
        it
        for it in items
        if isinstance(it, dict) and "compacted older turns" in str(it.get("content", ""))
    ]
    assert len(markers) == 1
    assert "note-1[auth]" in markers[0]["content"]  # index carried into context
    assert "DIGEST" in markers[0]["content"]  # digest carried into context


@pytest.mark.asyncio
async def test_compaction_preserves_call_output_pairs() -> None:
    # Orphan call in the old span (summarised away) and a fully-paired call in
    # the recent window. After compaction no dangling call/output may survive.
    task = _msg("user", "TASK")
    old = [_call("orphan"), *[_msg("assistant", "x" * 500) for _ in range(10)]]
    recent = [_call("kept"), _output("kept")]
    session = FakeSession([task, *old, *recent])

    await compact_session(session, keep_recent=2, target_tokens=50, summarize=_stub_summary)

    items = await session.get_items()
    call_ids = {i.get("call_id") for i in items if isinstance(i, dict) and "call_id" in i}
    # The kept pair survives; the orphan (its output was never present) is gone.
    assert "kept" in call_ids
    assert "orphan" not in call_ids
    # No function_call_output without a matching function_call.
    calls = {i["call_id"] for i in items if i.get("type") == "function_call"}
    outputs = {i["call_id"] for i in items if i.get("type") == "function_call_output"}
    assert outputs <= calls


@pytest.mark.asyncio
async def test_compaction_persists_so_resume_reloads_compacted() -> None:
    session = FakeSession(
        [_msg("user", "TASK"), *[_msg("assistant", "x" * 500) for _ in range(20)]]
    )
    before = len(await session.get_items())

    await compact_session(session, keep_recent=3, target_tokens=50, summarize=_stub_summary)

    # A fresh read (models a resume reopening the same session) sees the
    # compacted history, not the original — growth is actually bounded.
    reopened = FakeSession(await session.get_items())
    assert len(await reopened.get_items()) < before


@pytest.mark.asyncio
async def test_compaction_noop_when_under_target() -> None:
    session = FakeSession([_msg("user", "TASK"), _msg("assistant", "small")])
    changed = await compact_session(
        session, keep_recent=1, target_tokens=100_000, summarize=_stub_summary
    )
    assert changed is False
    assert len(await session.get_items()) == 2


@pytest.mark.asyncio
async def test_compaction_returns_false_when_task_plus_recent_dominate() -> None:
    # keep_recent covers everything after the task, so there is no old span to
    # summarise — the escalation signal (caller must shrink/hand off).
    session = FakeSession([_msg("user", "TASK"), _msg("assistant", "x" * 5000)])
    changed = await compact_session(
        session, keep_recent=5, target_tokens=1, summarize=_stub_summary
    )
    assert changed is False


@pytest.mark.asyncio
async def test_summarize_receives_only_old_span() -> None:
    task = _msg("user", "TASK")
    old = [_msg("assistant", f"OLD-{i}") for i in range(6)]
    recent = [_msg("assistant", "RECENT")]
    session = FakeSession([task, *old, *recent])
    seen: dict[str, Any] = {}

    async def _spy(old_items: list[Any], _index: str) -> str:
        seen["old"] = old_items
        return "D"

    await compact_session(session, keep_recent=1, target_tokens=10, summarize=_spy)

    contents = [i.get("content") for i in seen["old"]]
    assert "TASK" not in contents  # pinned task never summarised
    assert "RECENT" not in contents  # recency window never summarised
    assert all(str(c).startswith("OLD-") for c in contents)


# --- T3: render_blackboard_index ---------------------------------------------


@pytest.mark.asyncio
async def test_index_lists_ids_not_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "strix.tools.notes.tools.qa_notes_summary",
        lambda *_a, **_k: {
            "refs": [{"note_id": "note-7", "category": "auth", "tags": ["jwt"]}],
            "signals": ["secret body text that must not leak"],
        },
    )
    monkeypatch.setattr(
        "strix.tools.loot.tools.qa_loot_summary",
        lambda *_a, **_k: {
            "refs": [{"loot_id": "loot-3", "loot_type": "credential", "scope": "api"}],
            "signals": [],
        },
    )

    index = render_blackboard_index()

    assert "note-7" in index and "loot-3" in index  # IDs present
    assert "secret body text" not in index  # signals/bodies never leak


@pytest.mark.asyncio
async def test_index_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "strix.tools.notes.tools.qa_notes_summary",
        lambda *_a, **_k: {
            "refs": [{"note_id": f"n{i}", "category": "c", "tags": ["t"]} for i in range(10_000)],
            "signals": [],
        },
    )
    index = render_blackboard_index()
    assert len(index) <= 6_200  # hard ceiling regardless of store size


# --- maybe_compact_session (shared entry) ------------------------------------


def _filter(window: int, *, ratio: float, keep: int = 2) -> ContextLimitFilter:
    # Empty summarizer_model => summarize_span returns "" with no model call,
    # so the whole path is offline and deterministic.
    return ContextLimitFilter(
        configured_window=window,
        compaction_trigger_ratio=ratio,
        compaction_keep_recent=keep,
        summarizer_model="",
    )


@pytest.mark.asyncio
async def test_maybe_compact_skips_under_trigger() -> None:
    session = FakeSession([_msg("user", "TASK"), _msg("assistant", "small")])
    changed = await maybe_compact_session(session, context_filter=_filter(200_000, ratio=0.7))
    assert changed is False


@pytest.mark.asyncio
async def test_maybe_compact_fires_over_trigger_offline() -> None:
    big = [_msg("assistant", "x" * 4000) for _ in range(10)]
    session = FakeSession([_msg("user", "TASK"), *big])
    changed = await maybe_compact_session(session, context_filter=_filter(2_000, ratio=0.7, keep=2))
    assert changed is True  # index-only compaction (no model call) still fires
    assert len(await session.get_items()) < 1 + len(big)


@pytest.mark.asyncio
async def test_maybe_compact_disabled_when_ratio_zero() -> None:
    big = [_msg("assistant", "x" * 4000) for _ in range(10)]
    session = FakeSession([_msg("user", "TASK"), *big])
    changed = await maybe_compact_session(session, context_filter=_filter(2_000, ratio=0.0))
    assert changed is False


@pytest.mark.asyncio
async def test_maybe_compact_no_filter_or_session() -> None:
    assert await maybe_compact_session(None, context_filter=_filter(2_000, ratio=0.7)) is False
    assert await maybe_compact_session(FakeSession([]), context_filter=None) is False


# --- T6: thin-root ingested-summary cap --------------------------------------


def test_ingested_summary_is_clipped() -> None:
    from strix.tools.agents_graph.tools import _render_completion_report

    report = _render_completion_report(
        agent_name="child",
        agent_id="c1",
        task="t",
        success=True,
        result_summary="A" * 50_000,
        findings=["f1"],
        recommendations=[],
    )
    assert "summary clipped" in report
    assert len(report) < 10_000  # 50k prose does not enter the parent context
    assert "f1" in report  # structured findings are preserved


def test_short_summary_not_clipped() -> None:
    from strix.tools.agents_graph.tools import _render_completion_report

    report = _render_completion_report(
        agent_name="child",
        agent_id="c1",
        task="t",
        success=True,
        result_summary="short summary",
        findings=[],
        recommendations=[],
    )
    assert "short summary" in report
    assert "clipped" not in report

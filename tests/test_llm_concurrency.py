"""Tests for the optional LLM concurrency limit (STRIX_LLM_MAX_CONCURRENCY).

All offline: a fake inner model drives the wrapper and records peak concurrency;
no real provider, network, Docker, or Caido. See Specs/llm-concurrency/.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
from agents.models.multi_provider import MultiProvider

from strix.config import loader as settings_loader
from strix.config.models import (
    StrixProvider,
    _ConcurrencyLimitedModel,
    _llm_semaphore,
    _llm_semaphores,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class _FakeModel:
    """Inner model that records peak concurrency and echoes its call args."""

    def __init__(self, *, fail: bool = False, fail_stream_after: int | None = None) -> None:
        self.current = 0
        self.peak = 0
        self.fail = fail
        self.fail_stream_after = fail_stream_after
        self.closed = False
        self.last_args: tuple[Any, ...] = ()
        self.last_kwargs: dict[str, Any] = {}

    async def _enter(self) -> None:
        self.current += 1
        self.peak = max(self.peak, self.current)

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        self.last_args, self.last_kwargs = args, kwargs
        await self._enter()
        try:
            if self.fail:
                raise RuntimeError("boom")
            await asyncio.sleep(0.02)
            return "ok"
        finally:
            self.current -= 1

    async def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[int]:
        self.last_args, self.last_kwargs = args, kwargs
        await self._enter()
        try:
            for i in range(3):
                await asyncio.sleep(0.01)
                yield i
                if self.fail_stream_after is not None and i >= self.fail_stream_after:
                    raise RuntimeError("stream boom")
        finally:
            self.current -= 1

    async def close(self) -> None:
        self.closed = True

    def get_retry_advice(self, _request: Any) -> Any:
        return "advice"


class _StubSettings:
    """Minimal stand-in for Settings exposing only llm.max_concurrency."""

    def __init__(self, limit: int) -> None:
        self.llm = type("_LlmStub", (), {"max_concurrency": limit})()


def _stub_limit(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    # get_model does `from strix.config.loader import load_settings` at call time,
    # so patching the loader module's attribute is what takes effect.
    monkeypatch.setattr(settings_loader, "load_settings", lambda: _StubSettings(limit))


async def _drain(gen: AsyncIterator[int]) -> list[int]:
    return [event async for event in gen]


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the memoised loader cache and per-loop semaphores between tests."""
    monkeypatch.setattr(settings_loader, "_cached", None)
    monkeypatch.setattr(settings_loader, "_override", None)
    _llm_semaphores.clear()


# 1 — no-regression guard: unlimited => unwrapped model (identity).
async def test_get_model_unwrapped_when_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setattr(MultiProvider, "get_model", lambda *_: sentinel)
    for limit in (0, -1):
        _stub_limit(monkeypatch, limit)
        assert StrixProvider().get_model("openai/XXXX") is sentinel


# 2 — limited => wraps, keeping the inner model.
async def test_get_model_wraps_when_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setattr(MultiProvider, "get_model", lambda *_: sentinel)
    _stub_limit(monkeypatch, 2)
    wrapped = StrixProvider().get_model("openai/XXXX")
    assert isinstance(wrapped, _ConcurrencyLimitedModel)
    assert wrapped._inner is sentinel


# 3 — get_response caps concurrency.
async def test_get_response_caps_concurrency() -> None:
    fake = _FakeModel()
    wrapper = _ConcurrencyLimitedModel(fake, asyncio.Semaphore(2))
    results = await asyncio.gather(*(wrapper.get_response() for _ in range(5)))
    assert fake.peak == 2
    assert results == ["ok"] * 5


# 4 — stream_response caps concurrency.
async def test_stream_response_caps_concurrency() -> None:
    fake = _FakeModel()
    wrapper = _ConcurrencyLimitedModel(fake, asyncio.Semaphore(2))
    out = await asyncio.gather(*(_drain(wrapper.stream_response()) for _ in range(5)))
    assert fake.peak == 2
    assert all(o == [0, 1, 2] for o in out)


# 5 — slot held for the whole stream (two streams at limit 1 run serially).
async def test_stream_holds_slot_for_whole_stream() -> None:
    fake = _FakeModel()
    wrapper = _ConcurrencyLimitedModel(fake, asyncio.Semaphore(1))
    await asyncio.gather(_drain(wrapper.stream_response()), _drain(wrapper.stream_response()))
    assert fake.peak == 1


# 6 — a stream created but never iterated takes no slot.
async def test_stream_created_but_not_iterated_acquires_no_slot() -> None:
    fake = _FakeModel()
    sem = asyncio.Semaphore(1)
    wrapper = _ConcurrencyLimitedModel(fake, sem)
    gen = wrapper.stream_response()  # created, not iterated
    assert sem._value == 1
    assert await wrapper.get_response() == "ok"
    await gen.aclose()


# 7 — consumer abort (aclose) releases the slot.
async def test_slot_released_on_consumer_abort() -> None:
    fake = _FakeModel()
    sem = asyncio.Semaphore(1)
    wrapper = _ConcurrencyLimitedModel(fake, sem)
    gen = wrapper.stream_response()
    assert await gen.__anext__() == 0
    await gen.aclose()
    assert sem._value == 1
    assert await wrapper.get_response() == "ok"


# 8 — close() delegates to the inner model.
async def test_close_delegates_to_inner() -> None:
    fake = _FakeModel()
    wrapper = _ConcurrencyLimitedModel(fake, asyncio.Semaphore(1))
    await wrapper.close()
    assert fake.closed is True


# 9 — get_retry_advice() delegates to the inner model.
def test_get_retry_advice_delegates_to_inner() -> None:
    fake = _FakeModel()
    wrapper = _ConcurrencyLimitedModel(fake, asyncio.Semaphore(1))
    assert wrapper.get_retry_advice(object()) == "advice"


# 10 — one cap shared across two providers in the same loop (runner + reflection).
async def test_semaphore_shared_per_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MultiProvider, "get_model", lambda *_: _FakeModel())
    _stub_limit(monkeypatch, 2)
    w1 = StrixProvider().get_model("openai/XXXX")
    w2 = StrixProvider().get_model("openai/XXXX")
    assert isinstance(w1, _ConcurrencyLimitedModel)
    assert isinstance(w2, _ConcurrencyLimitedModel)
    assert w1._semaphore is w2._semaphore


# 11 — no cross-event-loop leak.
def test_semaphore_not_shared_across_loops() -> None:
    async def grab() -> asyncio.Semaphore | None:
        return _llm_semaphore(2)

    s1 = asyncio.run(grab())
    s2 = asyncio.run(grab())
    assert s1 is not None
    assert s2 is not None
    assert s1 is not s2


# 12 — None when uncapped.
async def test_semaphore_none_when_unlimited() -> None:
    assert _llm_semaphore(0) is None
    assert _llm_semaphore(-1) is None


# 13 — resolves from env and from the flat JSON env block; defaults to 0.
def test_settings_env_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRIX_LLM_MAX_CONCURRENCY", raising=False)
    monkeypatch.setattr(settings_loader, "_override", tmp_path / "absent.json")
    monkeypatch.setattr(settings_loader, "_cached", None)
    assert settings_loader.load_settings().llm.max_concurrency == 0

    monkeypatch.setenv("STRIX_LLM_MAX_CONCURRENCY", "3")
    monkeypatch.setattr(settings_loader, "_cached", None)
    assert settings_loader.load_settings().llm.max_concurrency == 3

    monkeypatch.delenv("STRIX_LLM_MAX_CONCURRENCY", raising=False)
    cfg = tmp_path / "cli-config.json"
    cfg.write_text(json.dumps({"env": {"STRIX_LLM_MAX_CONCURRENCY": 4}}), encoding="utf-8")
    settings_loader.apply_config_override(cfg)
    assert settings_loader.load_settings().llm.max_concurrency == 4


# 14 — args and results pass through verbatim.
async def test_wrapper_forwards_args_and_result() -> None:
    fake = _FakeModel()
    wrapper = _ConcurrencyLimitedModel(fake, asyncio.Semaphore(2))
    assert await wrapper.get_response("a", "b", x=1) == "ok"
    assert fake.last_args == ("a", "b")
    assert fake.last_kwargs == {"x": 1}
    assert await _drain(wrapper.stream_response("c", y=2)) == [0, 1, 2]
    assert fake.last_args == ("c",)
    assert fake.last_kwargs == {"y": 2}


# 15 — no-nesting/no-deadlock guard: serial calls progress at limit 1.
async def test_serial_calls_progress_at_limit_one() -> None:
    fake = _FakeModel()
    wrapper = _ConcurrencyLimitedModel(fake, asyncio.Semaphore(1))
    assert await wrapper.get_response() == "ok"
    assert await wrapper.get_response() == "ok"
    assert fake.peak == 1


# 16 — an inner get_response error frees the slot before any retry backoff.
async def test_slot_released_when_get_response_raises() -> None:
    fake = _FakeModel(fail=True)
    sem = asyncio.Semaphore(1)
    wrapper = _ConcurrencyLimitedModel(fake, sem)
    with pytest.raises(RuntimeError):
        await wrapper.get_response()
    assert sem._value == 1
    fake.fail = False
    assert await wrapper.get_response() == "ok"


# 17 — an inner stream error mid-way frees the slot.
async def test_slot_released_when_stream_raises_midway() -> None:
    fake = _FakeModel(fail_stream_after=0)
    sem = asyncio.Semaphore(1)
    wrapper = _ConcurrencyLimitedModel(fake, sem)
    got: list[int] = []
    with pytest.raises(RuntimeError):
        async for event in wrapper.stream_response():
            got.append(event)  # noqa: PERF401 — partial collect; the loop raises before completing
    assert got == [0]
    assert sem._value == 1
    fake.fail_stream_after = None
    assert await _drain(wrapper.stream_response()) == [0, 1, 2]

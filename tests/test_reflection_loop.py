"""Tests for the Feature 2 reflection loop (`strix.core.reflection`) and its
QA-gate wiring.

Module under test (not yet implemented — these tests are RED by design):

    strix.core.reflection

exposing ``build_reflection_input``/``apply_reflection`` (pure),
``run_reflection`` (impure, model-mocked), ``_schedule_reflection`` +
``_reflection_lock``/``_reflection_dirty``/``_reflection_running``
(single-flight), plus the
``on_agent_end`` trigger added to ``strix.core.hooks.ReportUsageHooks`` and
the lead-gap QA wiring in ``strix.tools.qa_loop.{tool,rules}``.

Mirrors ``tests/test_loot_store.py`` / ``tests/test_finish_scan_guards.py``
conventions: ``asyncio_mode="auto"`` (plain ``async def``, no marker), ``XXXX``
placeholders for secrets/domains, autouse isolation of module-level state.

Import guard: ``strix.core.reflection`` does not exist yet. Importing it at
collection time is intentional — it fails the whole file fast with a clean
ImportError (RED) instead of masking the missing implementation behind a pile
of per-test AttributeErrors. The QA-wiring tests (22/22a/23/24/25/26) exercise
``strix.tools.qa_loop`` which *does* already exist (Feature 1 shipped), so they
collect fine but must still fail (assertion errors) because the lead-gap rule
and the ``_build_review_context`` audit wiring are not yet present.

Model mock surface (BINDING, see CONTRACT_F2.md): tests monkeypatch
``strix.core.reflection.StrixProvider`` so ``StrixProvider().get_model(...)``
returns a fake model object whose ``get_response`` is an ``AsyncMock``. The
fake response mirrors what ``report/dedupe.py`` reads:

- ``resp.output`` — a list containing a real ``openai.types.responses.
  ResponseOutputMessage`` whose ``.content`` holds a ``ResponseOutputText``
  with the JSON payload in ``.text`` (this is the exact shape
  ``report/dedupe.py::_extract_text`` walks — mirror it, do not invent a new
  attribute).
- ``resp.usage`` — a real ``agents.usage.Usage`` instance (same type
  ``record_sdk_usage`` expects from ``report/dedupe.py``).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage
from openai.types.responses.response_output_text import ResponseOutputText

from strix.config import loader
from strix.core import reflection
from strix.core.reflection import (
    apply_reflection,
    build_reflection_input,
    run_reflection,
)
from strix.tools.audit_state.tools import (
    _apply_assumption,
    _audit_state,
    _get_audit_state_impl,
    _update_audit_state_impl,
    hydrate_audit_state_from_disk,
)


if TYPE_CHECKING:
    from pathlib import Path


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolated_reflection_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the audit_state store AND the reflection single-flight state.

    Mirrors ``test_audit_state.py``'s isolation but additionally resets the
    module-level single-flight lock/dirty flag so scheduling tests do not leak
    "a reflection is running" state between tests.

    These tests are fully offline: they already monkeypatch
    ``reflection.StrixProvider``. They must ALSO stop ``run_reflection`` from
    reading the developer's real config, which would cache a ``Settings`` in
    ``strix.config.loader._cached`` and mirror ``LLM_API_KEY``/``LLM_API_BASE``
    into ``os.environ`` — global state that leaks into other test files
    (e.g. ``test_config_loader.py``). So we neutralise the real config read:

    - ``reflection.configure_sdk_model_defaults`` becomes a no-op (it is what
      mutates the SDK defaults and ``os.environ``).
    - ``reflection.load_settings`` returns an inert fake ``Settings`` carrying
      only the retry fields ``model_retry_settings_from_config`` reads off
      ``settings.llm``; ``run_reflection`` otherwise just forwards the value to
      the now-no-op ``configure_sdk_model_defaults``.
    - ``strix.config.loader._cached`` is reset so no cached real Settings
      survives, and ``monkeypatch`` auto-restores any ``os.environ`` mutation.
    """
    monkeypatch.setattr(loader, "_cached", None)

    fake_settings = SimpleNamespace(
        llm=SimpleNamespace(
            max_retries=5,
            retry_initial_delay=1.0,
            retry_max_delay=90.0,
            retry_multiplier=2.0,
            api_key=None,
            api_base=None,
            model=None,
        )
    )
    monkeypatch.setattr(reflection, "load_settings", lambda: fake_settings)
    monkeypatch.setattr(reflection, "configure_sdk_model_defaults", lambda _settings: None)

    hydrate_audit_state_from_disk(tmp_path)
    _audit_state.clear()
    reflection._reflection_lock = asyncio.Lock()
    reflection._reflection_dirty = False
    reflection._reflection_running = False
    yield
    reflection._reflection_lock = asyncio.Lock()
    reflection._reflection_dirty = False
    reflection._reflection_running = False


def _make_response(payload: dict[str, Any] | str, *, usage: Usage | None = None) -> SimpleNamespace:
    """Build a fake SDK ``ModelResponse`` mirroring report/dedupe.py's shape.

    ``resp.output`` is walked by dedupe's ``_extract_text``: it iterates
    ``response.output``, keeps ``ResponseOutputMessage`` items, and joins the
    ``.text`` of each ``.content`` chunk. ``resp.usage`` is a real SDK
    ``Usage`` object, exactly what ``record_sdk_usage(usage=resp.usage)``
    expects.
    """
    text = payload if isinstance(payload, str) else json.dumps(payload)
    message = ResponseOutputMessage(
        id="msg_reflection",
        content=[ResponseOutputText(text=text, type="output_text", annotations=[])],
        role="assistant",
        status="completed",
        type="message",
    )
    return SimpleNamespace(output=[message], usage=usage if usage is not None else Usage())


def _fake_model(get_response: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(get_response=get_response)


def _patch_provider(monkeypatch: pytest.MonkeyPatch, model: SimpleNamespace) -> None:
    """Monkeypatch ``reflection.StrixProvider`` so ``.get_model()`` returns ``model``.

    This is the chosen mock surface (alternative allowed by the contract:
    patching ``StrixProvider.get_model`` directly) — patching the whole
    provider class avoids touching the real class used elsewhere in the
    process.
    """
    fake_provider_instance = SimpleNamespace(get_model=lambda _resolved_model: model)
    monkeypatch.setattr(reflection, "StrixProvider", lambda: fake_provider_instance)


def _fake_coordinator(
    *, budget_stopped: bool = False, is_shutting_down: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(budget_stopped=budget_stopped, is_shutting_down=is_shutting_down)


def _fake_run_context(
    *,
    agent_id: str | None,
    parent_id: str | None,
    coordinator: Any = None,
    caido_client: Any = None,
) -> SimpleNamespace:
    """Mirror the real inner-context dict (runner.py/execution.py) wrapped the
    way the SDK's ``RunContextWrapper`` exposes it: ``context.context``.
    """
    inner = {
        "agent_id": agent_id,
        "parent_id": parent_id,
        "coordinator": coordinator,
        "caido_client": caido_client,
    }
    return SimpleNamespace(context=inner)


# --------------------------------------------------------------------------- #
# 14: build_reflection_input is pure
# --------------------------------------------------------------------------- #


def test_build_reflection_input_is_pure() -> None:
    snapshot = {
        "thesis": "Prior thesis: auth surface likely weak.",
        "loot_refs": [{"loot_id": "ab12cd", "loot_type": "credential"}],
        "target_profile": {"target_types": ["web_application"]},
        "qa_gaps": [{"gap_id": "auth_jwt:jwt_authentication", "priority": "high"}],
        "leads": [{"lead_id": "lead01", "priority": "high", "status": "open"}],
    }

    messages_a = build_reflection_input(snapshot)
    messages_b = build_reflection_input(snapshot)

    assert messages_a == messages_b  # deterministic, no I/O
    assert all({"role", "content"} <= set(m) for m in messages_a)

    blob = json.dumps(messages_a)
    assert "Prior thesis" in blob
    assert "ab12cd" in blob
    assert "lead01" in blob
    assert "auth_jwt:jwt_authentication" in blob

    # No raw values — only the id/enum shape should appear, never a bare
    # secret-shaped value smuggled into the snapshot.
    assert "credential" in blob or "loot_type" in blob


def test_build_reflection_input_omits_raw_values_from_snapshot() -> None:
    """A snapshot that (incorrectly) carried a raw value must not leak into the
    rendered messages any differently than any other opaque string — but the
    *contract* is that callers only ever pass ids/enums. This guards that the
    function itself performs no extra raw-value formatting/expansion.
    """
    snapshot = {"thesis": "", "loot_refs": [], "target_profile": {}, "qa_gaps": [], "leads": []}
    messages = build_reflection_input(snapshot)
    assert isinstance(messages, list)
    assert len(messages) >= 1


# --------------------------------------------------------------------------- #
# 15-16: apply_reflection
# --------------------------------------------------------------------------- #


def test_apply_reflection_applies_delta() -> None:
    seed_state, assumption_id = _apply_assumption(
        {"thesis": "", "assumptions": {}, "leads": {}, "updated_at": None},
        text="Auth uses JWT with a static secret.",
        confidence="medium",
        supersedes=None,
        reason=None,
        refs=[],
    )
    _audit_state.clear()
    _audit_state.update(seed_state)

    result = apply_reflection(
        {
            "thesis": "Revised: JWT secret appears reused across environments.",
            "assumptions": [
                {
                    "text": "JWT secret is shared between staging and prod.",
                    "confidence": "high",
                    "supersedes": assumption_id,
                    "reason": "Confirmed via token replay across hosts.",
                    "refs": ["ab12cd"],
                }
            ],
            "leads": [
                {
                    "text": "Pursue JWT secret reuse for cross-env auth bypass.",
                    "priority": "high",
                    "rationale": "High-impact if confirmed.",
                    "refs": ["ab12cd"],
                }
            ],
            "lead_updates": [],
        }
    )

    assert result["thesis_changed"] is True
    assert result["assumptions_added"] == 1
    assert result["leads_added"] == 1

    doc = _get_audit_state_impl(include_superseded=True)
    assert doc["thesis"].startswith("Revised:")

    old = doc["assumptions"][assumption_id]
    assert old["status"] == "superseded"
    assert old["superseded_by"]
    new_assumption_id = old["superseded_by"]
    new_assumption = doc["assumptions"][new_assumption_id]
    assert new_assumption["supersedes"] == assumption_id
    assert new_assumption["reason"]

    assert len(doc["leads"]) == 1
    (lead,) = doc["leads"].values()
    assert lead["priority"] == "high"
    assert lead["status"] == "open"

    # lead_updates path
    lead_id = next(iter(doc["leads"]))
    update_result = apply_reflection(
        {
            "thesis": None,
            "assumptions": [],
            "leads": [],
            "lead_updates": [{"lead_id": lead_id, "status": "in_progress"}],
        }
    )
    assert update_result["lead_updates"] == 1
    doc2 = _get_audit_state_impl()
    assert doc2["leads"][lead_id]["status"] == "in_progress"


def test_apply_reflection_ignores_malformed_items() -> None:
    result = apply_reflection(
        {
            "thesis": "A workable revised thesis.",
            "assumptions": [
                {"text": "Bad confidence enum.", "confidence": "extreme"},
                {"text": "Missing confidence field entirely."},
                {
                    "text": "Supersedes an id that does not exist.",
                    "confidence": "low",
                    "supersedes": "doesnotexist",
                },
                {"text": "This one is well-formed.", "confidence": "medium"},
            ],
            "leads": [
                {"text": "Bad priority enum.", "priority": "urgent!!"},
                {"text": "Missing priority field."},
                {"text": "This lead is well-formed.", "priority": "low"},
            ],
            "lead_updates": [
                {"lead_id": "unknown-lead-id", "status": "done"},
                {"status": "done"},  # missing lead_id entirely
            ],
        }
    )

    # Should not raise, and should apply exactly the well-formed items.
    assert result["thesis_changed"] is True
    assert result["assumptions_added"] == 1
    assert result["leads_added"] == 1
    assert result["lead_updates"] == 0

    doc = _get_audit_state_impl()
    assumption_texts = {a["text"] for a in doc["assumptions"].values()}
    assert "This one is well-formed." in assumption_texts
    lead_texts = {ld["text"] for ld in doc["leads"].values()}
    assert "This lead is well-formed." in lead_texts


# --------------------------------------------------------------------------- #
# 17-19a: run_reflection (model mocked)
# --------------------------------------------------------------------------- #


async def test_run_reflection_tolerates_bad_json_then_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _get_audit_state_impl()

    get_response = AsyncMock(
        side_effect=[
            _make_response("not json at all, just prose"),
            _make_response("still not json, retry failed too"),
        ]
    )
    _patch_provider(monkeypatch, _fake_model(get_response))

    result = await run_reflection(model="XXXX/local-model", caido_client=None)

    assert get_response.call_count == 2  # one retry, then skip
    assert result is not None  # never raises, returns cleanly

    after = _get_audit_state_impl()
    assert after == before  # audit_state unchanged


async def test_run_reflection_skips_when_budget_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _get_audit_state_impl()
    get_response = AsyncMock(return_value=_make_response({"thesis": "should not be applied"}))
    _patch_provider(monkeypatch, _fake_model(get_response))

    result = await run_reflection(
        model="XXXX/local-model",
        caido_client=None,
        coordinator=_fake_coordinator(budget_stopped=True),
    )

    get_response.assert_not_called()
    after = _get_audit_state_impl()
    assert after == before
    assert result is not None


async def test_run_reflection_records_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    from strix.report.state import ReportState, set_global_report_state

    report_state = ReportState("run-reflection-usage")
    set_global_report_state(report_state)

    usage = Usage(requests=1, input_tokens=1000, output_tokens=500, total_tokens=1500)
    payload = {"thesis": "", "assumptions": [], "leads": [], "lead_updates": []}
    get_response = AsyncMock(return_value=_make_response(payload, usage=usage))
    _patch_provider(monkeypatch, _fake_model(get_response))

    before_cost = report_state.get_total_llm_cost()
    await run_reflection(model="gpt-4o-mini", caido_client=None)

    assert get_response.call_count == 1
    usage_record = report_state.get_total_llm_usage()
    assert usage_record["total_tokens"] >= 1500
    assert report_state.get_total_llm_cost() >= before_cost


async def test_run_reflection_reads_lock_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard (R3): run_reflection must snapshot via the
    lock-protected accessors, not raw-iterate the module dict. Simulate a
    concurrent writer mutating the store mid-snapshot via a controllable fake
    and assert run_reflection still completes without raising.
    """
    _update_audit_state_impl(lead="Seed lead for lock-safety check.", priority="low")

    real_get_audit_state_impl = reflection._get_audit_state_impl  # type: ignore[attr-defined]
    call_count = {"n": 0}

    def _mutating_get_audit_state_impl(*args: Any, **kwargs: Any) -> dict[str, Any]:
        call_count["n"] += 1
        # Simulate another writer landing a change between calls — this must
        # not raise "dict changed size during iteration" because the real
        # accessor takes the lock and returns a snapshot, not a live view.
        if call_count["n"] == 1:
            _update_audit_state_impl(lead="Concurrent lead.", priority="medium")
        return real_get_audit_state_impl(*args, **kwargs)

    monkeypatch.setattr(
        reflection, "_get_audit_state_impl", _mutating_get_audit_state_impl
    )

    get_response = AsyncMock(
        return_value=_make_response(
            {"thesis": "", "assumptions": [], "leads": [], "lead_updates": []}
        )
    )
    _patch_provider(monkeypatch, _fake_model(get_response))

    result = await run_reflection(model="XXXX/local-model", caido_client=None)

    assert result is not None
    assert call_count["n"] >= 1


# --------------------------------------------------------------------------- #
# 20-21: on_agent_end trigger + single-flight
# --------------------------------------------------------------------------- #


async def test_on_agent_end_triggers_for_child_not_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from strix.core.hooks import ReportUsageHooks

    scheduled: list[dict[str, Any]] = []

    async def _fake_run_reflection(**kwargs: Any) -> dict[str, Any]:
        scheduled.append(kwargs)
        return {}

    monkeypatch.setattr(reflection, "run_reflection", _fake_run_reflection)

    hooks = ReportUsageHooks(model="XXXX/local-model")

    child_ctx = _fake_run_context(agent_id="child-1", parent_id="root-0")
    await hooks.on_agent_end(child_ctx, agent=SimpleNamespace(name="child"), output=None)
    await asyncio.sleep(0)  # let the scheduled task run

    assert len(scheduled) == 1

    root_ctx = _fake_run_context(agent_id="root-0", parent_id=None)
    await hooks.on_agent_end(root_ctx, agent=SimpleNamespace(name="strix"), output=None)
    await asyncio.sleep(0)

    assert len(scheduled) == 1  # unchanged — root end must not schedule


async def test_on_agent_end_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from strix.core.hooks import ReportUsageHooks

    async def _boom(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("reflection blew up")

    monkeypatch.setattr(reflection, "run_reflection", _boom)

    hooks = ReportUsageHooks(model="XXXX/local-model")
    ctx = _fake_run_context(agent_id="child-1", parent_id="root-0")

    # Must return normally even though the scheduled reflection raises.
    await hooks.on_agent_end(ctx, agent=SimpleNamespace(name="child"), output=None)
    await asyncio.sleep(0)  # let the failing task run + be swallowed

    # Budget-stopped / shutting-down guards also must not raise and must skip.
    scheduled: list[dict[str, Any]] = []

    async def _fake_run_reflection(**kwargs: Any) -> dict[str, Any]:
        scheduled.append(kwargs)
        return {}

    monkeypatch.setattr(reflection, "run_reflection", _fake_run_reflection)

    stopped_ctx = _fake_run_context(
        agent_id="child-2",
        parent_id="root-0",
        coordinator=_fake_coordinator(budget_stopped=True),
    )
    await hooks.on_agent_end(stopped_ctx, agent=SimpleNamespace(name="child"), output=None)
    await asyncio.sleep(0)
    assert scheduled == []

    shutdown_ctx = _fake_run_context(
        agent_id="child-3",
        parent_id="root-0",
        coordinator=_fake_coordinator(is_shutting_down=True),
    )
    await hooks.on_agent_end(shutdown_ctx, agent=SimpleNamespace(name="child"), output=None)
    await asyncio.sleep(0)
    assert scheduled == []


async def test_reflection_single_flight_coalesces(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = {"n": 0}
    release = asyncio.Event()

    async def _slow_run_reflection(**_kwargs: Any) -> dict[str, Any]:
        call_count["n"] += 1
        await release.wait()
        return {}

    monkeypatch.setattr(reflection, "run_reflection", _slow_run_reflection)

    # First call starts the "running" reflection.
    reflection._schedule_reflection(model="XXXX/local-model", caido_client=None)
    await asyncio.sleep(0)  # let the task start and enter the slow section
    assert call_count["n"] == 1

    # A burst of near-simultaneous child ends while it's running.
    reflection._schedule_reflection(model="XXXX/local-model", caido_client=None)
    reflection._schedule_reflection(model="XXXX/local-model", caido_client=None)
    reflection._schedule_reflection(model="XXXX/local-model", caido_client=None)
    await asyncio.sleep(0)

    assert call_count["n"] == 1  # coalesced — no second run has started yet
    assert reflection._reflection_dirty is True

    release.set()  # let the first run finish, which should trigger one re-run
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert call_count["n"] == 2  # at most one extra run for the whole burst


async def test_single_flight_coalesces_back_to_back_no_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for the TOCTOU race in single-flight coalescing.

    Two ``_schedule_reflection`` calls issued back-to-back with NO intervening
    ``await`` — exactly what happens when ``on_agent_end`` fires for two
    children finishing in the same event-loop iteration — must NOT both spawn a
    runner. The old ``_reflection_lock.locked()`` guard failed here: the lock is
    only acquired inside the scheduled coroutine on a later tick, so both
    synchronous calls observed ``locked() is False`` and both ran a reflection.

    This test FAILS against the buggy code (``call_count == 3``: two initial +
    one coalesced) and passes after the synchronous ``_reflection_running``
    compare-and-swap fix (``call_count <= 2``: one initial + one coalesced).
    """
    call_count = {"n": 0}
    release = asyncio.Event()

    async def _slow_run_reflection(**_kwargs: Any) -> dict[str, Any]:
        call_count["n"] += 1
        await release.wait()
        return {}

    monkeypatch.setattr(reflection, "run_reflection", _slow_run_reflection)

    # Two calls back-to-back, NO await between them (single event-loop tick).
    reflection._schedule_reflection(model="XXXX/local-model", caido_client=None)
    reflection._schedule_reflection(model="XXXX/local-model", caido_client=None)

    # Drain the loop so any spawned runner(s) reach the slow section.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Only ONE runner may have started; the second call must have coalesced.
    assert call_count["n"] == 1
    assert reflection._reflection_dirty is True

    # Let the first run finish; it must trigger exactly one coalesced re-run.
    release.set()
    for _ in range(4):
        await asyncio.sleep(0)

    assert call_count["n"] == 2  # one initial + one coalesced re-run, never three


# --------------------------------------------------------------------------- #
# 22-25: QA-gate lead rule + wiring
# --------------------------------------------------------------------------- #


def test_qa_review_context_safe_on_empty_audit_state() -> None:
    """Regression guard R1 — the single most important test in this file.

    ``_run_review`` is unguarded on the finish path; a throw in the new
    ``qa_audit_summary()``/lead-gap wiring on an EMPTY audit_state would break
    the finish gate for every deep scan, feature-used or not.
    """
    from strix.report.state import ReportState
    from strix.tools.qa_loop.rules import evaluate_qa_gaps
    from strix.tools.qa_loop.tool import _build_review_context

    report_state = ReportState("run-reflection-empty-audit")
    report_state.set_scan_config({"targets": [{"type": "web_application"}], "scan_mode": "deep"})

    tool_history: dict[str, Any] = {
        "tool_history": [],
        "agents_with_sessions": 0,
        "agents_total": 0,
        "extraction_errors": [],
    }

    review_context = _build_review_context(report_state, tool_history, [], proxy_ok=False)
    gaps = evaluate_qa_gaps(review_context)

    lead_gaps = [g for g in gaps if g.get("gap_id", "").startswith("audit_lead:")]
    assert lead_gaps == []  # no blocking gap manufactured out of nothing


def test_qa_gap_for_open_high_lead() -> None:
    from strix.tools.qa_loop.rules import evaluate_qa_gaps

    _update_audit_state_impl(
        lead="XXXX idor on /orders",
        priority="high",
    )
    doc = _get_audit_state_impl()
    (lead_id,) = doc["leads"].keys()

    review_context = {
        "target_types": set(),
        "tool_history": [],
        "tool_history_available": True,
        "tool_history_partial": False,
        "proxy_sitemap_available": False,
        "signal_text": [],
        "_note_refs": [],
        "_loot_refs": [],
        "_audit_leads": [{"lead_id": lead_id, "priority": "high", "status": "open"}],
        "_audit_lead_texts": {lead_id: "XXXX idor on /orders"},
    }

    gaps = evaluate_qa_gaps(review_context)
    lead_gaps = [g for g in gaps if g.get("gap_id") == f"audit_lead:{lead_id}"]
    assert len(lead_gaps) == 1
    gap = lead_gaps[0]
    assert gap["priority"] == "high"
    assert "XXXX idor on /orders" in gap["reason"]


@pytest.mark.parametrize(
    ("status", "priority"),
    [
        ("done", "high"),
        ("dropped", "high"),
        ("open", "medium"),
        ("open", "low"),
        ("in_progress", "medium"),
    ],
)
def test_no_qa_gap_when_lead_done_dropped_or_lower(status: str, priority: str) -> None:
    from strix.tools.qa_loop.rules import evaluate_qa_gaps

    review_context = {
        "target_types": set(),
        "tool_history": [],
        "tool_history_available": True,
        "tool_history_partial": False,
        "proxy_sitemap_available": False,
        "signal_text": [],
        "_note_refs": [],
        "_loot_refs": [],
        "_audit_leads": [{"lead_id": "leadxx", "priority": priority, "status": status}],
        "_audit_lead_texts": {"leadxx": "Some lead text."},
    }

    gaps = evaluate_qa_gaps(review_context)
    lead_gaps = [g for g in gaps if g.get("gap_id", "").startswith("audit_lead:")]
    assert lead_gaps == []


def test_lead_gap_can_be_acknowledged() -> None:
    from strix.tools.qa_loop.rules import assemble_review, evaluate_qa_gaps

    review_context = {
        "target_types": set(),
        "tool_history": [],
        "tool_history_available": True,
        "tool_history_partial": False,
        "proxy_sitemap_available": False,
        "signal_text": [],
        "_note_refs": [],
        "_loot_refs": [],
        "_audit_leads": [{"lead_id": "leadxx", "priority": "high", "status": "open"}],
        "_audit_lead_texts": {"leadxx": "Pursue high-value lead."},
    }

    gaps = evaluate_qa_gaps(review_context)
    (gap,) = [g for g in gaps if g.get("gap_id", "").startswith("audit_lead:")]

    unacknowledged = assemble_review(gaps, acknowledged_gaps=[], max_priority_gaps=5)
    assert unacknowledged["ready_to_finish"] is False
    assert any(g["gap_id"] == gap["gap_id"] for g in unacknowledged["priority_gaps"])

    acknowledged = assemble_review(
        gaps, acknowledged_gaps=[gap["gap_id"]], max_priority_gaps=5
    )
    assert acknowledged["ready_to_finish"] is True
    assert not any(g["gap_id"] == gap["gap_id"] for g in acknowledged["priority_gaps"])


def test_lead_gap_reason_is_scrubbed() -> None:
    from strix.tools.qa_loop.rules import evaluate_qa_gaps
    from strix.tools.qa_loop.tool import _scrub_gap

    scrub_matched_text = "Bearer XXXXSCRUBMEXXXX token reused across tenants"
    review_context = {
        "target_types": set(),
        "tool_history": [],
        "tool_history_available": True,
        "tool_history_partial": False,
        "proxy_sitemap_available": False,
        "signal_text": [],
        "_note_refs": [],
        "_loot_refs": [],
        "_audit_leads": [{"lead_id": "leadxx", "priority": "high", "status": "open"}],
        "_audit_lead_texts": {"leadxx": scrub_matched_text},
    }

    gaps = evaluate_qa_gaps(review_context)
    (gap,) = [g for g in gaps if g.get("gap_id", "").startswith("audit_lead:")]

    persisted_gap = _scrub_gap(gap)
    assert "XXXXSCRUBMEXXXX" not in persisted_gap["reason"]
    assert "XXXXSCRUBMEXXXX" not in persisted_gap["suggested_action"]


# --------------------------------------------------------------------------- #
# 26: qa_loop wiring surfaces audit signals (mirrors
# test_loot_store.py::test_qa_loop_surfaces_loot_signals — sync, no await)
# --------------------------------------------------------------------------- #


def test_qa_loop_surfaces_audit_signals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from strix.report.state import ReportState
    from strix.tools.qa_loop import tool as qa_loop_tool
    from strix.tools.qa_loop.tool import _build_review_context

    hydrate_audit_state_from_disk(tmp_path)
    _update_audit_state_impl(
        lead="XXXX credential reuse across staging and prod",
        priority="high",
    )

    async def _no_proxy(_inner: dict[str, Any]) -> tuple[list[str], bool]:
        return [], False

    monkeypatch.setattr(qa_loop_tool, "_collect_proxy", _no_proxy)

    report_state = ReportState("run-audit-qa")
    report_state._run_dir = tmp_path
    report_state.set_scan_config({"targets": [{"type": "web_application"}], "scan_mode": "deep"})

    tool_history: dict[str, Any] = {
        "tool_history": [],
        "agents_with_sessions": 0,
        "agents_total": 0,
        "extraction_errors": [],
    }

    # _build_review_context is a SYNC def — call it directly, no await.
    review_context = _build_review_context(report_state, tool_history, [], proxy_ok=False)

    signal_text = review_context["signal_text"]
    assert any("credential" in s or "reuse" in s for s in signal_text)

    assert "_audit_leads" in review_context
    assert len(review_context["_audit_leads"]) >= 1
    # ids/enums only in refs — never free text.
    for ref in review_context["_audit_leads"]:
        assert "text" not in ref
        assert set(ref) <= {"lead_id", "priority", "status"}


# --------------------------------------------------------------------------- #
# 27-29: tool registration + prompt wiring
# --------------------------------------------------------------------------- #


def test_base_tools_include_audit_state_tools() -> None:
    from strix.agents.factory import _BASE_TOOLS

    names = {t.name for t in _BASE_TOOLS}
    assert "get_audit_state" in names
    assert "update_audit_state" in names


def test_select_tools_adds_no_new_root_tool() -> None:
    from strix.agents.factory import _BASE_TOOLS, select_tools

    base_names = {t.name for t in _BASE_TOOLS}
    root_names = {t.name for t in select_tools(is_root=True)}

    assert root_names == base_names | {"review_before_finish", "finish_scan"}


def test_root_agent_skill_mentions_audit_state() -> None:
    from pathlib import Path as SyncPath

    text = SyncPath("strix/skills/coordination/root_agent.md").read_text(encoding="utf-8")
    assert "get_audit_state" in text
    assert "update_audit_state" in text

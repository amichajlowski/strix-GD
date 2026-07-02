"""Tests for the audit-state store (`strix.tools.audit_state.tools`).

Module under test (not yet implemented — these tests are RED by design):

    strix.tools.audit_state.tools

exposing ``get_audit_state``/``update_audit_state`` (``@function_tool``
wrappers), ``_get_audit_state_impl``/``_update_audit_state_impl`` (pure-ish
sync impls), ``_apply_assumption``/``_apply_lead``/``_update_lead`` (pure
helpers), ``hydrate_audit_state_from_disk``, ``_persist``, ``qa_audit_summary``,
and the module-level ``_audit_state``/``_audit_state_path``.

Mirrors ``tests/test_loot_store.py`` / ``strix/tools/notes/tools.py``
conventions: ``XXXX`` placeholders for secrets/domains, and autouse isolation
of module-level state between tests. Seeding goes through the ``_impl``
helpers after ``hydrate_audit_state_from_disk(tmp_path)`` — never through the
async ``@function_tool`` wrappers directly (see
Specs/adaptive-audit/CONTRACT.md).

Import guard: the module under test does not exist yet. Importing it at
collection time is intentional — it makes the whole file fail fast with a
clean ImportError (RED) instead of masking the missing implementation behind
a pile of per-test AttributeErrors.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from strix.tools.audit_state import tools as audit_state_tools
from strix.tools.audit_state.tools import (
    _apply_assumption,
    _apply_lead,
    _audit_state,
    _get_audit_state_impl,
    _persist,
    _update_audit_state_impl,
    _update_lead,
    get_audit_state,
    hydrate_audit_state_from_disk,
    qa_audit_summary,
    update_audit_state,
)


if TYPE_CHECKING:
    from pathlib import Path


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolated_audit_state(tmp_path: Path) -> None:
    """Point the module-level store at a scratch dir and clear it per test.

    Mirrors how ``tests/test_loot_store.py`` isolates the loot store: hydrate
    from a fresh ``tmp_path`` (which also clears ``_audit_state``) so no state
    leaks between tests.
    """
    hydrate_audit_state_from_disk(tmp_path)
    _audit_state.clear()


# --------------------------------------------------------------------------- #
# 1: update_audit_state creates thesis + assumption + lead
# --------------------------------------------------------------------------- #


def test_update_creates_thesis_assumption_lead() -> None:
    result = _update_audit_state_impl(
        thesis="Laravel API behind Cloudflare; auth is bearer-token.",
        assumption="No WAF in front of the origin",
        confidence="medium",
        lead="IDOR on /orders/{id} using object_id from loot",
        priority="high",
    )

    assert result["success"] is True
    assumption_id = result["assumption_id"]
    lead_id = result["lead_id"]
    assert assumption_id
    assert lead_id

    state = _get_audit_state_impl()
    assert state["thesis"] == "Laravel API behind Cloudflare; auth is bearer-token."
    assert assumption_id in state["assumptions"]
    assert state["assumptions"][assumption_id]["confidence"] == "medium"
    assert state["assumptions"][assumption_id]["status"] == "active"
    assert lead_id in state["leads"]
    assert state["leads"][lead_id]["priority"] == "high"
    assert state["leads"][lead_id]["status"] == "open"

    assert "counts" in result


# --------------------------------------------------------------------------- #
# 2: unknown enums rejected
# --------------------------------------------------------------------------- #


def test_update_rejects_unknown_enums() -> None:
    bad_confidence = _update_audit_state_impl(
        assumption="No WAF in front of the origin",
        confidence="not_a_real_confidence",
    )
    assert bad_confidence["success"] is False
    for valid in ("low", "medium", "high"):
        assert valid in bad_confidence["error"]

    bad_priority = _update_audit_state_impl(
        lead="IDOR on /orders/{id}",
        priority="not_a_real_priority",
    )
    assert bad_priority["success"] is False
    for valid in ("low", "medium", "high"):
        assert valid in bad_priority["error"]

    seeded = _update_audit_state_impl(
        lead="IDOR on /orders/{id}",
        priority="high",
    )
    lead_id = seeded["lead_id"]

    bad_status = _update_audit_state_impl(
        lead_id=lead_id,
        lead_status="not_a_real_status",
    )
    assert bad_status["success"] is False
    for valid in ("open", "in_progress", "done", "dropped"):
        assert valid in bad_status["error"]


# --------------------------------------------------------------------------- #
# 3: companion fields required
# --------------------------------------------------------------------------- #


def test_update_requires_confidence_with_assumption_and_priority_with_lead() -> None:
    missing_confidence = _update_audit_state_impl(assumption="No WAF in front of the origin")
    assert missing_confidence["success"] is False
    assert "confidence" in missing_confidence["error"].lower()

    missing_priority = _update_audit_state_impl(lead="IDOR on /orders/{id}")
    assert missing_priority["success"] is False
    assert "priority" in missing_priority["error"].lower()


# --------------------------------------------------------------------------- #
# 4-5: supersede
# --------------------------------------------------------------------------- #


def test_supersede_marks_old_and_links() -> None:
    first = _update_audit_state_impl(
        assumption="No WAF in front of the origin",
        confidence="medium",
    )
    old_id = first["assumption_id"]

    second = _update_audit_state_impl(
        assumption="WAF added after httpx run triggered a challenge",
        confidence="high",
        supersedes=old_id,
        reason="initial httpx run showed no challenge; later runs did",
    )
    assert second["success"] is True
    new_id = second["assumption_id"]
    assert new_id != old_id

    state = _get_audit_state_impl(include_superseded=True)
    old_entry = state["assumptions"][old_id]
    new_entry = state["assumptions"][new_id]

    assert old_entry["status"] == "superseded"
    assert old_entry["superseded_by"] == new_id
    assert new_entry["supersedes"] == old_id
    assert new_entry["reason"] == "initial httpx run showed no challenge; later runs did"
    assert new_entry["status"] == "active"


def test_supersede_unknown_or_already_superseded_errors() -> None:
    unknown = _update_audit_state_impl(
        assumption="Some new assumption",
        confidence="low",
        supersedes="doesnotexist",
    )
    assert unknown["success"] is False
    assert "error" in unknown
    state_after_unknown = _get_audit_state_impl(include_superseded=True)
    assert state_after_unknown["assumptions"] == {}

    first = _update_audit_state_impl(
        assumption="No WAF in front of the origin",
        confidence="medium",
    )
    old_id = first["assumption_id"]
    second = _update_audit_state_impl(
        assumption="WAF added",
        confidence="high",
        supersedes=old_id,
        reason="observed challenge",
    )
    assert second["success"] is True

    third = _update_audit_state_impl(
        assumption="Trying to supersede an already-superseded assumption",
        confidence="high",
        supersedes=old_id,
        reason="should fail",
    )
    assert third["success"] is False
    assert "error" in third

    # No mutation from the failed attempt: old assumption still points at the
    # same superseder it already had.
    state = _get_audit_state_impl(include_superseded=True)
    assert state["assumptions"][old_id]["superseded_by"] == second["assumption_id"]


# --------------------------------------------------------------------------- #
# 6: get_audit_state hides superseded by default
# --------------------------------------------------------------------------- #


def test_get_audit_state_hides_superseded_by_default() -> None:
    first = _update_audit_state_impl(
        assumption="No WAF in front of the origin",
        confidence="medium",
    )
    old_id = first["assumption_id"]
    second = _update_audit_state_impl(
        assumption="WAF added",
        confidence="high",
        supersedes=old_id,
        reason="observed challenge",
    )
    new_id = second["assumption_id"]

    default_view = _get_audit_state_impl()
    assert old_id not in default_view["assumptions"]
    assert new_id in default_view["assumptions"]

    full_view = _get_audit_state_impl(include_superseded=True)
    assert old_id in full_view["assumptions"]
    assert new_id in full_view["assumptions"]


# --------------------------------------------------------------------------- #
# 7: lead status transitions
# --------------------------------------------------------------------------- #


def test_update_lead_status_transitions() -> None:
    created = _update_audit_state_impl(
        lead="IDOR on /orders/{id}",
        priority="high",
    )
    lead_id = created["lead_id"]

    in_progress = _update_audit_state_impl(lead_id=lead_id, lead_status="in_progress")
    assert in_progress["success"] is True
    state = _get_audit_state_impl()
    assert state["leads"][lead_id]["status"] == "in_progress"

    done = _update_audit_state_impl(lead_id=lead_id, lead_status="done")
    assert done["success"] is True
    state = _get_audit_state_impl()
    assert state["leads"][lead_id]["status"] == "done"

    unknown = _update_audit_state_impl(lead_id="doesnotexist", lead_status="open")
    assert unknown["success"] is False
    assert "error" in unknown


# --------------------------------------------------------------------------- #
# 8: bounded strings/collections
# --------------------------------------------------------------------------- #


def test_strings_and_collections_bounded() -> None:
    oversized_thesis = "T" * 5000
    oversized_text = "A" * 2000
    many_refs = [f"ref{i}" for i in range(100)]
    oversized_ref = "R" * 200

    result = _update_audit_state_impl(
        thesis=oversized_thesis,
        assumption=oversized_text,
        confidence="low",
        refs=[*many_refs, oversized_ref],
    )
    assert result["success"] is True

    state = _get_audit_state_impl()
    assert len(state["thesis"]) <= 1000

    assumption_id = result["assumption_id"]
    entry = state["assumptions"][assumption_id]
    assert len(entry["text"]) <= 512
    assert len(entry["refs"]) <= 32
    assert all(len(r) <= 64 for r in entry["refs"])

    # Active assumptions capped at 200.
    for i in range(210):
        _update_audit_state_impl(assumption=f"assumption {i}", confidence="low")
    state = _get_audit_state_impl()
    assert len(state["assumptions"]) <= 200

    # Leads capped at 200.
    for i in range(210):
        _update_audit_state_impl(lead=f"lead {i}", priority="low")
    state = _get_audit_state_impl()
    assert len(state["leads"]) <= 200


def test_superseded_history_cap_evicts_oldest() -> None:
    # Build a long chain of supersedes so most assumptions end up superseded,
    # then confirm the total is capped and the oldest superseded entries are
    # evicted first rather than growing unbounded.
    prev_id = None
    created_ids: list[str] = []
    for i in range(1100):
        result = _update_audit_state_impl(
            assumption=f"assumption {i}",
            confidence="low",
            supersedes=prev_id,
            reason="chained supersede" if prev_id else None,
        )
        assert result["success"] is True
        prev_id = result["assumption_id"]
        created_ids.append(prev_id)

    state = _get_audit_state_impl(include_superseded=True)
    total = len(state["assumptions"])
    assert total <= 1000

    # The very first created assumption (oldest superseded) should have been
    # evicted once the cap was exceeded.
    assert created_ids[0] not in state["assumptions"]
    # The most recent (still active) assumption must survive.
    assert created_ids[-1] in state["assumptions"]


# --------------------------------------------------------------------------- #
# 9-10: persist / hydrate roundtrip + malformed json
# --------------------------------------------------------------------------- #


def test_persist_and_hydrate_roundtrip(tmp_path: Path) -> None:
    hydrate_audit_state_from_disk(tmp_path)
    result = _update_audit_state_impl(
        thesis="Laravel API behind Cloudflare.",
        assumption="No WAF in front of the origin",
        confidence="medium",
        lead="IDOR on /orders/{id}",
        priority="high",
    )
    assumption_id = result["assumption_id"]
    lead_id = result["lead_id"]
    _persist()

    audit_state_path = tmp_path / "audit_state.json"
    assert audit_state_path.exists()

    hydrate_audit_state_from_disk(tmp_path)
    state = _get_audit_state_impl()
    assert state["thesis"] == "Laravel API behind Cloudflare."
    assert assumption_id in state["assumptions"]
    assert lead_id in state["leads"]


def test_hydrate_handles_malformed_json(tmp_path: Path) -> None:
    audit_state_path = tmp_path / "audit_state.json"
    audit_state_path.write_text("{ this is not valid json !!!", encoding="utf-8")

    hydrate_audit_state_from_disk(tmp_path)

    state = _get_audit_state_impl()
    assert state["thesis"] == ""
    assert state["assumptions"] == {}
    assert state["leads"] == {}


# --------------------------------------------------------------------------- #
# 11: qa_audit_summary refs are ids only
# --------------------------------------------------------------------------- #


def test_qa_audit_summary_refs_are_ids_only() -> None:
    _update_audit_state_impl(
        thesis="Laravel API behind Cloudflare; bearer-token auth XXXX.",
        assumption="No WAF in front of the origin XXXX",
        confidence="medium",
        lead="IDOR on /orders/{id} using object_id XXXX",
        priority="high",
    )

    summary = qa_audit_summary()
    refs_blob = json.dumps(summary["refs"])

    assert "text" not in refs_blob
    for ref in summary["refs"]:
        assert set(ref.keys()) <= {
            "lead_id",
            "priority",
            "status",
            "assumption_id",
            "confidence",
        }

    lead_refs = [r for r in summary["refs"] if "lead_id" in r]
    assert lead_refs
    for ref in lead_refs:
        assert set(ref.keys()) == {"lead_id", "priority", "status"}

    assumption_refs = [r for r in summary["refs"] if "assumption_id" in r]
    assert assumption_refs
    for ref in assumption_refs:
        assert set(ref.keys()) == {"assumption_id", "confidence"}

    # Free text only appears in signals (lowercased), never in refs.
    assert any("idor" in s for s in summary["signals"])
    assert "IDOR" not in refs_blob


# --------------------------------------------------------------------------- #
# 11a: write-time scrub backstop
# --------------------------------------------------------------------------- #


def test_update_audit_state_scrubs_free_text() -> None:
    secret_shaped_thesis = "Origin token is Bearer abcDEF123.token-XXXX for the API."  # noqa: S105
    secret_shaped_lead = "Use leaked Authorization: Bearer abcDEF123token to hit /orders"  # noqa: S105
    secret_shaped_reason = "found via Bearer abcDEF123token in logs"  # noqa: S105

    result = _update_audit_state_impl(
        thesis=secret_shaped_thesis,
        lead=secret_shaped_lead,
        priority="high",
        reason=secret_shaped_reason,
    )
    assert result["success"] is True

    state = _get_audit_state_impl()
    assert "abcDEF123" not in state["thesis"]
    assert "Bearer XXXX" in state["thesis"] or "XXXX" in state["thesis"]

    lead_id = result["lead_id"]
    lead_entry = state["leads"][lead_id]
    assert "abcDEF123" not in lead_entry["text"]
    assert "abcDEF123" not in json.dumps(lead_entry)

    # Persisted doc must never contain the raw secret-shaped token either.
    persisted_blob = json.dumps(state)
    assert "abcDEF123" not in persisted_blob


# --------------------------------------------------------------------------- #
# 12: empty store is safe
# --------------------------------------------------------------------------- #


def test_get_audit_state_empty_store_is_safe() -> None:
    state = _get_audit_state_impl()
    assert state["success"] is True
    assert state["thesis"] == ""
    assert state["assumptions"] == {}
    assert state["leads"] == {}


# --------------------------------------------------------------------------- #
# 13: ids not values
# --------------------------------------------------------------------------- #


def test_audit_state_stores_ids_not_values() -> None:
    result = _update_audit_state_impl(
        assumption="No WAF in front of the origin",
        confidence="medium",
        refs=["ab12cd"],
    )
    assert result["success"] is True
    assumption_id = result["assumption_id"]

    state = _get_audit_state_impl()
    assert state["assumptions"][assumption_id]["refs"] == ["ab12cd"]

    lead_result = _update_audit_state_impl(
        lead="IDOR on /orders/{id}",
        priority="high",
        refs=["ab12cd"],
    )
    lead_id = lead_result["lead_id"]
    state = _get_audit_state_impl()
    assert state["leads"][lead_id]["refs"] == ["ab12cd"]

    summary = qa_audit_summary()
    for ref in summary["refs"]:
        assert "value" not in ref
        assert "refs" not in ref


# --------------------------------------------------------------------------- #
# Pure-helper direct coverage (per CONTRACT.md — these are the unit-test
# targets called out by the spec, exercised independently of the _impl glue).
# --------------------------------------------------------------------------- #


def test_apply_assumption_is_pure_and_returns_new_state() -> None:
    state: dict[str, Any] = {"thesis": "", "assumptions": {}, "leads": {}, "updated_at": None}
    new_state, assumption_id = _apply_assumption(
        state,
        text="No WAF in front of the origin",
        confidence="medium",
        supersedes=None,
        reason=None,
        refs=None,
    )
    assert state["assumptions"] == {}
    assert assumption_id in new_state["assumptions"]


def test_apply_lead_is_pure_and_returns_new_state() -> None:
    state: dict[str, Any] = {"thesis": "", "assumptions": {}, "leads": {}, "updated_at": None}
    new_state, lead_id = _apply_lead(
        state,
        text="IDOR on /orders/{id}",
        priority="high",
        rationale=None,
        refs=None,
    )
    assert state["leads"] == {}
    assert lead_id in new_state["leads"]
    assert new_state["leads"][lead_id]["status"] == "open"


def test_update_lead_is_pure_and_raises_on_unknown_id() -> None:
    state: dict[str, Any] = {"thesis": "", "assumptions": {}, "leads": {}, "updated_at": None}
    _, lead_id = _apply_lead(
        state,
        text="IDOR on /orders/{id}",
        priority="high",
        rationale=None,
        refs=None,
    )
    state["leads"] = {
        lead_id: {
            "lead_id": lead_id,
            "text": "IDOR on /orders/{id}",
            "priority": "high",
            "status": "open",
            "rationale": None,
            "refs": [],
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
        }
    }

    new_state = _update_lead(state, lead_id=lead_id, status="in_progress", priority=None)
    assert new_state["leads"][lead_id]["status"] == "in_progress"
    # original untouched
    assert state["leads"][lead_id]["status"] == "open"

    with pytest.raises(ValueError, match="doesnotexist"):
        _update_lead(state, lead_id="doesnotexist", status="open", priority=None)


# --------------------------------------------------------------------------- #
# Sanity: tools module actually re-exports what's expected (guards typos in
# this test file itself, not the implementation).
# --------------------------------------------------------------------------- #


def test_audit_state_tools_module_shape() -> None:
    assert hasattr(audit_state_tools, "_audit_state")
    assert hasattr(audit_state_tools, "_audit_state_path")
    assert callable(get_audit_state.on_invoke_tool)
    assert callable(update_audit_state.on_invoke_tool)

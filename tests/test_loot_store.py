"""Tests for the structured loot store (`strix.tools.loot.tools`).

Module under test (not yet implemented — these tests are RED by design):

    strix.tools.loot.tools

exposing ``record_loot``/``get_loot``/``delete_loot`` (``@function_tool``
wrappers), ``_record_loot_impl``/``_get_loot_impl``/``_delete_loot_impl``
(pure impls), ``hydrate_loot_from_disk``, ``_persist``, ``mask_value``,
``qa_loot_summary``, and the module-level ``_loot_storage``/``_loot_path``.

Mirrors ``tests/test_qa_loop_review.py`` / ``strix/tools/notes/tools.py``
conventions: ``asyncio_mode="auto"`` (plain ``async def``, no marker), ``XXXX``
placeholders for secrets/domains, and autouse isolation of module-level state
between tests.
"""

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING, Any

import pytest


if TYPE_CHECKING:
    from pathlib import Path

# --------------------------------------------------------------------------- #
# Import guard
# --------------------------------------------------------------------------- #
#
# The module under test does not exist yet. Importing it at collection time is
# intentional: it makes the whole file fail fast with a clean ImportError
# (RED) instead of masking the missing implementation behind a pile of
# per-test AttributeErrors.

from strix.tools.loot import tools as loot_tools
from strix.tools.loot.tools import (
    _delete_loot_impl,
    _get_loot_impl,
    _loot_storage,
    _persist,
    _record_loot_impl,
    delete_loot,
    get_loot,
    hydrate_loot_from_disk,
    mask_value,
    qa_loot_summary,
    record_loot,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolated_loot_state(tmp_path: Path) -> None:
    """Point the module-level store at a scratch dir and clear it per test.

    Mirrors how ``test_qa_loop_review.py`` isolates notes/todos: hydrate from
    a fresh ``tmp_path`` (which also clears ``_loot_storage``) so no state
    leaks between tests.
    """
    hydrate_loot_from_disk(tmp_path)
    _loot_storage.clear()


# --------------------------------------------------------------------------- #
# 11-13: record_loot / dedup / validation
# --------------------------------------------------------------------------- #


def test_record_loot_creates_entry() -> None:
    result = _record_loot_impl(
        loot_type="credential",
        value="admin:XXXX",
        source="login form /admin",
    )
    assert result["success"] is True
    assert result["deduped"] is False
    loot_id = result["loot_id"]
    assert loot_id
    assert loot_id in _loot_storage
    assert _loot_storage[loot_id]["value"] == "admin:XXXX"


def test_record_loot_rejects_unknown_type() -> None:
    result = _record_loot_impl(
        loot_type="not_a_real_type",
        value="admin:XXXX",
        source="login form /admin",
    )
    assert result["success"] is False
    valid_types = [
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
    for valid_type in valid_types:
        assert valid_type in result["error"]


def test_record_loot_dedups_on_type_value_scope() -> None:
    first = _record_loot_impl(
        loot_type="credential",
        value="admin:XXXX",
        source="login form /admin",
        scope="api.XXXX.example",
        tags=["admin"],
    )
    assert first["deduped"] is False

    second = _record_loot_impl(
        loot_type="credential",
        value="admin:XXXX",
        source="second login form /login",
        scope="api.XXXX.example",
        tags=["reuse"],
    )
    assert second["success"] is True
    assert second["deduped"] is True
    assert second["loot_id"] == first["loot_id"]

    assert len(_loot_storage) == 1
    entry = _loot_storage[first["loot_id"]]
    assert "admin" in entry["tags"]
    assert "reuse" in entry["tags"]


# --------------------------------------------------------------------------- #
# 14-15: get_loot filters / raw values
# --------------------------------------------------------------------------- #


def test_get_loot_filters_compose() -> None:
    _record_loot_impl(
        loot_type="credential",
        value="admin:XXXX",
        source="login /admin",
        scope="api.XXXX.example",
        tags=["admin", "reuse"],
    )
    _record_loot_impl(
        loot_type="credential",
        value="user:XXXX",
        source="login /login",
        scope="api.XXXX.example",
        tags=["user"],
    )
    _record_loot_impl(
        loot_type="token",
        value="Bearer XXXX",
        source="login /admin",
        scope="api.XXXX.example",
        tags=["admin", "reuse"],
    )
    _record_loot_impl(
        loot_type="credential",
        value="other:XXXX",
        source="other source",
        scope="other.XXXX.example",
        tags=["admin", "reuse"],
    )

    result = _get_loot_impl(loot_type="credential", tags=["admin"], search="login")

    assert result["success"] is True
    values = {entry["value"] for entry in result["results"]}
    assert values == {"admin:XXXX"}


def test_get_loot_returns_raw_values() -> None:
    _record_loot_impl(
        loot_type="secret",
        value="jwt-signing-secret-XXXX",
        source="config leak",
    )
    result = _get_loot_impl()
    assert result["success"] is True
    assert any(entry["value"] == "jwt-signing-secret-XXXX" for entry in result["results"])


# --------------------------------------------------------------------------- #
# 16: bounded strings
# --------------------------------------------------------------------------- #


def test_loot_strings_are_bounded() -> None:
    oversized_value = "V" * 5000
    oversized_source = "S" * 1000
    many_tags = [f"tag{i}" for i in range(50)]

    result = _record_loot_impl(
        loot_type="secret",
        value=oversized_value,
        source=oversized_source,
        tags=many_tags,
    )
    assert result["success"] is True
    entry = _loot_storage[result["loot_id"]]
    assert len(entry["value"]) <= 4096
    assert len(entry["source"]) <= 512
    assert len(entry["tags"]) <= 32


# --------------------------------------------------------------------------- #
# 17: delete_loot
# --------------------------------------------------------------------------- #


def test_delete_loot_removes_entry() -> None:
    created = _record_loot_impl(
        loot_type="cookie",
        value="session=XXXX",
        source="browser devtools",
    )
    loot_id = created["loot_id"]

    result = _delete_loot_impl(loot_id)
    assert result["success"] is True
    assert loot_id not in _loot_storage

    not_found = _delete_loot_impl(loot_id)
    assert not_found["success"] is False
    assert "error" in not_found


# --------------------------------------------------------------------------- #
# 18-19: persist / hydrate roundtrip + malformed json
# --------------------------------------------------------------------------- #


def test_loot_persist_and_hydrate_roundtrip(tmp_path: Path) -> None:
    hydrate_loot_from_disk(tmp_path)
    result = _record_loot_impl(
        loot_type="internal_host",
        value="10.0.0.5",
        source="nmap scan",
    )
    loot_id = result["loot_id"]
    _persist()

    loot_path = tmp_path / "loot.json"
    assert loot_path.exists()
    assert loot_path.stat().st_mode & 0o777 == 0o600

    hydrate_loot_from_disk(tmp_path)
    assert loot_id in _loot_storage
    assert _loot_storage[loot_id]["value"] == "10.0.0.5"


def test_hydrate_handles_malformed_loot_json(tmp_path: Path) -> None:
    loot_path = tmp_path / "loot.json"
    loot_path.write_text("{ this is not valid json !!!", encoding="utf-8")

    hydrate_loot_from_disk(tmp_path)

    assert _loot_storage == {}


# --------------------------------------------------------------------------- #
# 20-21: qa_loot_summary / mask_value
# --------------------------------------------------------------------------- #


def test_qa_loot_summary_has_no_raw_values_or_source() -> None:
    _record_loot_impl(
        loot_type="credential",
        value="admin:supersecretpassword123",
        source="login form at /admin/login/portal",
        scope="api.XXXX.example",
        tags=["admin", "reuse"],
    )

    summary = qa_loot_summary()
    blob = json.dumps(summary["refs"])

    assert "supersecretpassword123" not in blob
    assert "login form at /admin/login/portal" not in blob

    ref = summary["refs"][0]
    assert ref["loot_type"] == "credential"
    assert ref["scope"] == "api.XXXX.example"
    assert "value" not in ref
    assert "source" not in ref

    # signals may carry lowercased loot_type/tags for rule inspection, but
    # never the raw secret value.
    assert "supersecretpassword123" not in json.dumps(summary["signals"])


def test_mask_value_redacts() -> None:
    secret = "supersecretpassword123"  # noqa: S105  # test placeholder, not a real secret
    masked = mask_value(secret)
    assert secret not in masked
    assert "len=" in masked


# --------------------------------------------------------------------------- #
# 21a: tool param is loot_type, not type
# --------------------------------------------------------------------------- #


def test_record_loot_param_is_loot_type() -> None:
    impl_params = inspect.signature(_record_loot_impl).parameters
    assert "loot_type" in impl_params
    assert "type" not in impl_params

    schema = record_loot.params_json_schema
    properties = schema.get("properties", {})
    assert "loot_type" in properties
    assert "type" not in properties


# --------------------------------------------------------------------------- #
# 21b: qa_loop wiring
# --------------------------------------------------------------------------- #


async def test_qa_loop_surfaces_loot_signals(tmp_path: Path) -> None:
    from strix.report.state import ReportState
    from strix.tools.qa_loop.tool import _build_review_context

    hydrate_loot_from_disk(tmp_path)
    _record_loot_impl(
        loot_type="credential",
        value="admin:XXXX",
        source="login /admin",
        tags=["reuse"],
    )

    report_state = ReportState("run-loot-qa")
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

    assert "_loot_refs" in review_context
    assert len(review_context["_loot_refs"]) >= 1


# --------------------------------------------------------------------------- #
# Sanity: tools module actually re-exports what's expected (guards typos in
# this test file itself, not the implementation).
# --------------------------------------------------------------------------- #


def test_loot_tools_module_shape() -> None:
    assert hasattr(loot_tools, "_loot_storage")
    assert hasattr(loot_tools, "_loot_path")
    assert callable(get_loot.on_invoke_tool)
    assert callable(delete_loot.on_invoke_tool)

"""Unit tests for the per-target fingerprint store (`strix.tools.target_profile.tools`).

Module under test (not yet implemented — these tests are RED by design):

    strix.tools.target_profile.tools

exposing the pure helpers ``classify_network_location`` and
``profile_to_hints``, the ``set_target_profile``/``get_target_profile``
``@function_tool`` wrappers, their pure impls ``_set_target_profile_impl`` /
``_get_target_profile_impl``, ``hydrate_target_profiles_from_disk``,
``_persist``, and the module-level ``_target_profiles`` dict.

Also covers the recon skill file
``strix/skills/reconnaissance/environment_profiling.md`` (must resolve via
``strix.skills.load_skills`` by filename stem) and the `_BASE_TOOLS`
registration of all six Feature 1-3 tools (traffic_health, loot store,
target profile).

Mirrors ``tests/test_loot_store.py`` / ``strix/tools/notes/tools.py``
conventions: ``asyncio_mode="auto"`` (plain ``async def``, no marker), ``XXXX``
placeholders for secrets/domains, and autouse isolation of module-level state
between tests. No DNS, no network calls — ``classify_network_location`` is
pure string/IP parsing only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

from strix.tools.target_profile.tools import (
    _get_target_profile_impl as get_target_profile_impl,
)
from strix.tools.target_profile.tools import (
    _persist,
    _target_profiles,
    classify_network_location,
    get_target_profile,
    hydrate_target_profiles_from_disk,
    profile_to_hints,
    set_target_profile,
)
from strix.tools.target_profile.tools import (
    _set_target_profile_impl as set_target_profile_impl,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolated_target_profile_state(tmp_path: Path) -> None:
    """Point the module-level store at a scratch dir and clear it per test.

    Mirrors how ``test_loot_store.py`` isolates loot state: hydrate from a
    fresh ``tmp_path`` (which also clears/repopulates ``_target_profiles``)
    then explicitly clear so no state leaks between tests.
    """
    hydrate_target_profiles_from_disk(tmp_path)
    _target_profiles.clear()


# --------------------------------------------------------------------------- #
# 22-25: classify_network_location
# --------------------------------------------------------------------------- #


def test_classify_localhost_variants() -> None:
    for host in ("localhost", "127.0.0.1", "::1", "x.local"):
        assert classify_network_location(host) == "localhost"


def test_classify_rfc1918_and_ula_internal() -> None:
    for host in ("10.0.0.5", "172.16.3.4", "192.168.1.1", "fc00::1"):
        assert classify_network_location(host) == "internal"


def test_classify_public_internet() -> None:
    for host in ("api.XXXX.example", "8.8.8.8"):
        assert classify_network_location(host) == "internet"


def test_classify_garbage_unknown() -> None:
    for junk in ("", "   ", "!!!not a host@@@", "http://"):
        assert classify_network_location(junk) == "unknown"


# --------------------------------------------------------------------------- #
# 26-29: profile_to_hints
# --------------------------------------------------------------------------- #


def test_profile_to_hints_localhost_high_throughput_no_evasion() -> None:
    hints = profile_to_hints({"network_location": "localhost"})
    assert hints["throughput"] == "high"
    assert hints["evasion"] == "none"
    assert hints["host_timeout"] == "short"


def test_profile_to_hints_waf_lowers_throughput_and_adds_evasion() -> None:
    hints = profile_to_hints({"network_location": "internet", "waf": "cloudflare"})
    assert hints["throughput"] == "low"
    assert hints["evasion"] == "encode+rotate-headers"


def test_profile_to_hints_graphql_suggests_graphql_skill() -> None:
    hints = profile_to_hints({"asset_type": "api_graphql"})
    assert "graphql" in hints["skills"]


def test_profile_to_hints_unknown_profile_conservative_defaults() -> None:
    for profile in ({}, {"network_location": "unknown"}):
        hints = profile_to_hints(profile)
        assert hints["throughput"] == "moderate"
        assert hints["evasion"] == "none"


# --------------------------------------------------------------------------- #
# 30-31: set_target_profile merge / enum validation
# --------------------------------------------------------------------------- #


def test_set_target_profile_merges_without_clobbering() -> None:
    first = set_target_profile_impl(
        target="api.XXXX.example",
        tech_stack=["nginx", "php", "laravel"],
        sources=["httpx"],
    )
    assert first["success"] is True

    second = set_target_profile_impl(
        target="api.XXXX.example",
        waf="cloudflare",
        sources=["wafw00f"],
    )
    assert second["success"] is True

    profile = _target_profiles["api.XXXX.example"]
    assert profile["tech_stack"] == ["nginx", "php", "laravel"]
    assert profile["waf"] == "cloudflare"

    sources = profile["sources"]
    assert "httpx" in sources
    assert "wafw00f" in sources
    assert len(sources) == len(set(sources))


def test_set_target_profile_drops_invalid_ports() -> None:
    result = set_target_profile_impl(
        target="api.XXXX.example",
        ports=[443, -1, 0, 99999, 8443],
    )
    assert result["success"] is True
    assert _target_profiles["api.XXXX.example"]["ports"] == [443, 8443]


def test_set_target_profile_validates_enums() -> None:
    result = set_target_profile_impl(
        target="api.XXXX.example",
        asset_type="not_a_real_asset_type",
    )
    assert result["success"] is False
    valid_asset_types = ["web_app", "spa", "api_rest", "api_graphql", "unknown"]
    for valid_type in valid_asset_types:
        assert valid_type in result["error"]


# --------------------------------------------------------------------------- #
# 32: get_target_profile facts + hints / all-profiles mode
# --------------------------------------------------------------------------- #


def test_get_target_profile_returns_facts_and_hints() -> None:
    set_target_profile_impl(
        target="api.XXXX.example",
        network_location="internet",
        waf="cloudflare",
    )

    single = get_target_profile_impl(target="api.XXXX.example")
    assert single["success"] is True
    assert "profile" in single
    assert "hints" in single
    assert single["profile"]["target"] == "api.XXXX.example"
    assert single["hints"]["evasion"] == "encode+rotate-headers"

    set_target_profile_impl(target="internal.XXXX.example", network_location="internal")

    all_profiles = get_target_profile_impl(target=None)
    assert all_profiles["success"] is True
    assert "profile" in all_profiles
    assert "api.XXXX.example" in all_profiles["profile"]
    assert "internal.XXXX.example" in all_profiles["profile"]


# --------------------------------------------------------------------------- #
# 33: persist / hydrate roundtrip
# --------------------------------------------------------------------------- #


def test_profile_persist_and_hydrate_roundtrip(tmp_path: Path) -> None:
    hydrate_target_profiles_from_disk(tmp_path)
    set_target_profile_impl(
        target="api.XXXX.example",
        network_location="internet",
        waf="cloudflare",
    )
    _persist()

    profile_path = tmp_path / "target_profiles.json"
    assert profile_path.exists()

    hydrate_target_profiles_from_disk(tmp_path)
    assert "api.XXXX.example" in _target_profiles
    assert _target_profiles["api.XXXX.example"]["waf"] == "cloudflare"


# --------------------------------------------------------------------------- #
# 34: recon skill discoverable via skills loader
# --------------------------------------------------------------------------- #


async def test_environment_profiling_skill_loads() -> None:
    from strix.skills import load_skills

    loaded = load_skills(["environment_profiling"])
    assert "environment_profiling" in loaded
    assert loaded["environment_profiling"].strip() != ""


# --------------------------------------------------------------------------- #
# 35: _BASE_TOOLS registration (base-tools check covering all Feature 1-3
# tools: traffic_health + loot store already exist; only the two
# target_profile tools are expected to be missing before this feature lands).
# --------------------------------------------------------------------------- #


async def test_base_tools_include_new_tools() -> None:
    from strix.agents.factory import _BASE_TOOLS

    tool_names = {tool.name for tool in _BASE_TOOLS}  # type: ignore[attr-defined]
    expected = {
        "traffic_health",
        "record_loot",
        "get_loot",
        "delete_loot",
        "set_target_profile",
        "get_target_profile",
    }
    assert expected.issubset(tool_names)


# --------------------------------------------------------------------------- #
# Sanity: tool wrappers are real FunctionTools (guards typos in this test
# file itself, not the implementation).
# --------------------------------------------------------------------------- #


def test_target_profile_tools_are_callable() -> None:
    assert callable(set_target_profile.on_invoke_tool)  # type: ignore[attr-defined]
    assert callable(get_target_profile.on_invoke_tool)  # type: ignore[attr-defined]

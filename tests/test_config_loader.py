"""Tests for persist_current() round-tripping resolved settings back to disk."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from strix.config import loader as settings_loader


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _load_from(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_loader, "_override", config_path)
    monkeypatch.setattr(settings_loader, "_cached", None)
    settings_loader.load_settings()


def test_persist_current_preserves_file_only_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STRIX_LLM sourced purely from the JSON file (never exported to os.environ)
    must survive a persist_current() round-trip, not be silently dropped."""
    config_path = tmp_path / "cli-config.json"
    config_path.write_text(
        json.dumps({"env": {"STRIX_LLM": "openai/deepseek-v4"}}),
        encoding="utf-8",
    )
    _load_from(config_path, monkeypatch)

    settings_loader.persist_current()

    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["env"]["STRIX_LLM"] == "openai/deepseek-v4"


def test_persist_current_prefers_canonical_alias_over_os_environ_mirror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field resolved from the JSON file can get mirrored into os.environ under a
    *different* alias by unrelated SDK-compat code (e.g. api_base -> OPENAI_BASE_URL,
    done by configure_sdk_model_defaults() at startup). persist_current() must still
    write the canonical native key (LLM_API_BASE), not whichever alias happens to be
    sitting in os.environ.
    """
    config_path = tmp_path / "cli-config.json"
    config_path.write_text(
        json.dumps({"env": {"LLM_API_BASE": "http://192.168.100.54:8000/v1"}}),
        encoding="utf-8",
    )
    _load_from(config_path, monkeypatch)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://192.168.100.54:8000/v1")

    settings_loader.persist_current()

    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["env"]["LLM_API_BASE"] == "http://192.168.100.54:8000/v1"
    assert "OPENAI_BASE_URL" not in written["env"]


def test_persist_current_drops_unset_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fields that were never set (no default worth persisting, e.g. an unset
    optional API key) should not show up as empty/None entries in the file."""
    config_path = tmp_path / "cli-config.json"
    config_path.write_text(json.dumps({"env": {"STRIX_LLM": "openai/deepseek-v4"}}), encoding="utf-8")
    _load_from(config_path, monkeypatch)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    settings_loader.persist_current()

    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert "LLM_API_KEY" not in written["env"]
    assert "OPENAI_API_KEY" not in written["env"]

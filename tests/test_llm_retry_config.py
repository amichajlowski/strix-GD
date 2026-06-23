"""Tests for configurable LLM retry settings."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from strix.config import loader as settings_loader
from strix.config.models import model_retry_settings_from_config
from strix.config.settings import Settings
from strix.core.inputs import make_model_settings


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


_RETRY_ENV_KEYS = (
    "STRIX_LLM_MAX_RETRIES",
    "STRIX_LLM_RETRY_INITIAL_DELAY",
    "STRIX_LLM_RETRY_MAX_DELAY",
    "STRIX_LLM_RETRY_MULTIPLIER",
)


def _clear_retry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _RETRY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_retry_settings_defaults_match_existing_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_retry_env(monkeypatch)

    retry = model_retry_settings_from_config(Settings())

    assert retry.max_retries == 5
    assert retry.backoff.initial_delay == 2.0
    assert retry.backoff.max_delay == 90.0
    assert retry.backoff.multiplier == 2.0
    assert retry.backoff.jitter is False


def test_retry_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRIX_LLM_MAX_RETRIES", "9")
    monkeypatch.setenv("STRIX_LLM_RETRY_INITIAL_DELAY", "1.5")
    monkeypatch.setenv("STRIX_LLM_RETRY_MAX_DELAY", "45")
    monkeypatch.setenv("STRIX_LLM_RETRY_MULTIPLIER", "3")

    retry = model_retry_settings_from_config(Settings())

    assert retry.max_retries == 9
    assert retry.backoff.initial_delay == 1.5
    assert retry.backoff.max_delay == 45.0
    assert retry.backoff.multiplier == 3.0


def test_retry_settings_read_config_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_retry_env(monkeypatch)
    config_path = tmp_path / "cli-config.json"
    config_path.write_text(
        json.dumps(
            {
                "env": {
                    "STRIX_LLM_MAX_RETRIES": "8",
                    "STRIX_LLM_RETRY_INITIAL_DELAY": "3.25",
                    "STRIX_LLM_RETRY_MAX_DELAY": "120",
                    "STRIX_LLM_RETRY_MULTIPLIER": "1.75",
                },
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings_loader, "_override", config_path)
    monkeypatch.setattr(settings_loader, "_cached", None)

    retry = model_retry_settings_from_config(settings_loader.load_settings())

    assert retry.max_retries == 8
    assert retry.backoff.initial_delay == 3.25
    assert retry.backoff.max_delay == 120.0
    assert retry.backoff.multiplier == 1.75


def test_make_model_settings_uses_configured_retry_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRIX_LLM_MAX_RETRIES", "4")
    settings = Settings()
    retry = model_retry_settings_from_config(settings)

    model_settings = make_model_settings(
        None,
        model_name="openai/gpt-5.4",
        retry_settings=retry,
    )

    assert model_settings.retry is retry

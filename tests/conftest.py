"""Shared test fixtures.

``_restore_global_process_state`` prevents cross-test leakage of two pieces of
module-global state that several tests mutate transitively:

- ``os.environ`` — ``strix.config.models.configure_sdk_model_defaults`` mirrors
  resolved config into the process environment (``OPENAI_BASE_URL``,
  ``LLM_API_KEY`` via ``setdefault``, ...). Any test that reaches the runner /
  reflection / model-call path (e.g. ``test_runner_resume``,
  ``test_agent_snapshots``, ``test_runtime_evidence``) leaks those entries,
  which then breaks ``test_config_loader`` when it runs later in the same
  session (ordering-dependent).
- ``strix.config.loader._cached`` — the settings cache; a leaked cache would
  hand a later test the wrong resolved ``Settings``.

Snapshotting/restoring both around every test makes the suite order-independent.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _restore_global_process_state() -> None:
    env_snapshot = dict(os.environ)
    try:
        from strix.config import loader as _loader
    except Exception:  # noqa: BLE001 - config package may be unimportable in isolated unit runs
        _loader = None

    yield

    os.environ.clear()
    os.environ.update(env_snapshot)
    if _loader is not None:
        _loader._cached = None

"""Tests for the startup RLIMIT_NOFILE guard in strix.interface.main."""

from __future__ import annotations

import resource

import pytest

from strix.interface.main import _FD_SOFT_MINIMUM, _raise_fd_limit


def _patch_rlimit(
    monkeypatch: pytest.MonkeyPatch, soft: int, hard: int
) -> list[tuple[int, int]]:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(resource, "getrlimit", lambda _res: (soft, hard))
    monkeypatch.setattr(resource, "setrlimit", lambda _res, limits: calls.append(limits))
    return calls


def test_raises_soft_limit_when_below_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_rlimit(monkeypatch, soft=256, hard=resource.RLIM_INFINITY)
    _raise_fd_limit()
    assert calls == [(_FD_SOFT_MINIMUM, resource.RLIM_INFINITY)]


def test_noop_when_already_high(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_rlimit(monkeypatch, soft=100_000, hard=resource.RLIM_INFINITY)
    _raise_fd_limit()
    assert calls == []


def test_noop_when_soft_is_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_rlimit(monkeypatch, soft=resource.RLIM_INFINITY, hard=resource.RLIM_INFINITY)
    _raise_fd_limit()
    assert calls == []


def test_caps_at_hard_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    # A finite hard cap below the target must not be exceeded.
    calls = _patch_rlimit(monkeypatch, soft=256, hard=4096)
    _raise_fd_limit()
    assert calls == [(4096, 4096)]


def test_never_raises_on_setrlimit_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resource, "getrlimit", lambda _res: (256, resource.RLIM_INFINITY))

    def _boom(_res: int, _limits: tuple[int, int]) -> None:
        raise OSError("nope")

    monkeypatch.setattr(resource, "setrlimit", _boom)
    _raise_fd_limit()  # must not propagate

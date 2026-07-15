"""SDK model configuration helpers."""

from __future__ import annotations

import asyncio
import os
import re
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from agents import set_default_openai_api, set_default_openai_key, set_tracing_disabled
from agents.models.interface import (
    Model,  # concrete import — _ConcurrencyLimitedModel subclasses it
)
from agents.models.multi_provider import MultiProvider
from agents.retry import (
    ModelRetryBackoffSettings,
    ModelRetrySettings,
    RetryDecision,
    RetryPolicyContext,
    retry_policies,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agents.models.interface import ModelProvider

    from strix.config.settings import Settings


# One semaphore per event loop: shared across the runner's StrixProvider and the
# reflection StrixProvider in a scan; WeakKeyDict so it does not outlive a test's
# event loop. ponytail: per-loop singleton, keyed by the running loop.
_llm_semaphores: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    WeakKeyDictionary()
)


def _llm_semaphore(limit: int) -> asyncio.Semaphore | None:
    """Return the shared per-loop semaphore, or None when uncapped (limit <= 0)."""
    if limit <= 0:
        return None
    loop = asyncio.get_running_loop()
    sem = _llm_semaphores.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(limit)
        _llm_semaphores[loop] = sem
    return sem


class _ConcurrencyLimitedModel(Model):
    """Wrap a Model so each request holds one slot of a shared semaphore."""

    def __init__(self, inner: Model, semaphore: asyncio.Semaphore) -> None:
        self._inner = inner
        self._semaphore = semaphore

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        async with self._semaphore:
            return await self._inner.get_response(*args, **kwargs)

    async def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        # Hold the slot for the WHOLE stream: the provider connection stays open
        # until the last event. Released on normal completion or if the consumer
        # closes/aborts the generator (async-with __aexit__ runs on aclose).
        # Acquire happens on the first __anext__, so a stream created but never
        # iterated takes no slot.
        async with self._semaphore:
            async for event in self._inner.stream_response(*args, **kwargs):
                yield event

    async def close(self) -> None:
        await self._inner.close()

    def get_retry_advice(self, request: Any) -> Any:
        return self._inner.get_retry_advice(request)


class StrixProvider(MultiProvider):
    """Route any non-OpenAI prefix through LiteLLM with the prefix preserved,
    so users type ``deepseek/deepseek-chat`` rather than
    ``litellm/deepseek/deepseek-chat``.
    """

    def _resolve_prefixed_model(
        self,
        *,
        original_model_name: str,
        prefix: str,
        stripped_model_name: str | None,
    ) -> tuple[ModelProvider, str | None]:
        if prefix in {"openai", "litellm", "any-llm"}:
            return super()._resolve_prefixed_model(
                original_model_name=original_model_name,
                prefix=prefix,
                stripped_model_name=stripped_model_name,
            )
        if prefix == "ollama" and stripped_model_name:
            return self._get_fallback_provider("litellm"), f"ollama_chat/{stripped_model_name}"
        return self._get_fallback_provider("litellm"), original_model_name

    def get_model(self, model_name: str | None) -> Model:
        model = super().get_model(model_name)
        from strix.config.loader import load_settings  # local import: avoids import cycle

        semaphore = _llm_semaphore(load_settings().llm.max_concurrency)
        if semaphore is None:
            return model
        return _ConcurrencyLimitedModel(model, semaphore)


# OpenAI's Responses/Chat streaming path raises the mid-stream rate-limit as a
# bare ``openai.APIError`` (see openai/_streaming.py) with NO ``status_code`` —
# so http_status((429,...)) and provider_suggested() both miss it and the agent
# is killed instead of backing off. Match it by message and honour the
# provider's "try again in Xs" hint.
_RATE_LIMIT_SIGNATURE = re.compile(r"rate limit reached|tokens per min|requests per min", re.I)
_TRY_AGAIN_DELAY = re.compile(r"try again in\s+([\d.]+)\s*(ms|s)", re.I)


def _parse_retry_delay(message: str) -> float | None:
    m = _TRY_AGAIN_DELAY.search(message)
    if not m:
        return None
    value = float(m.group(1))
    return value / 1000 if m.group(2).lower() == "ms" else value


def retry_in_stream_rate_limit(context: RetryPolicyContext) -> bool | RetryDecision:
    """Retry mid-stream rate-limit errors that carry no HTTP status code."""
    if context.normalized.status_code is not None:
        return False  # a real HTTP status: leave it to http_status()/provider_suggested()
    message = str(context.error)
    if not _RATE_LIMIT_SIGNATURE.search(message):
        return False
    return RetryDecision(retry=True, delay=_parse_retry_delay(message))


def _build_model_retry_settings(
    *,
    max_retries: int,
    initial_delay: float,
    max_delay: float,
    multiplier: float,
) -> ModelRetrySettings:
    return ModelRetrySettings(
        max_retries=max_retries,
        backoff=ModelRetryBackoffSettings(
            initial_delay=initial_delay,
            max_delay=max_delay,
            multiplier=multiplier,
            jitter=False,
        ),
        policy=retry_policies.any(
            retry_policies.provider_suggested(),
            retry_policies.network_error(),
            retry_policies.http_status((429, 500, 502, 503, 504)),
            retry_in_stream_rate_limit,
        ),
    )


DEFAULT_MODEL_RETRY = _build_model_retry_settings(
    max_retries=5,
    initial_delay=2.0,
    max_delay=90.0,
    multiplier=2.0,
)

RECOMMENDED_MODEL_NAMES = (
    "openai/gpt-5.6",
    "openai/gpt-5.6-sol",
    "openai/gpt-5.6-terra",
    "openai/gpt-5.5",
    "openai/gpt-5.5-pro",
    "openai/gpt-5.4",
    "openai/gpt-5.3-codex",
    "anthropic/claude-fable-5",
    "anthropic/claude-opus-4-8",
    "anthropic/claude-opus-4-7",
    "anthropic/claude-sonnet-5",
    "anthropic/claude-sonnet-4-6",
    "vertex_ai/gemini-3.1-pro-preview",
    "gemini/gemini-3.1-pro-preview",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-flash",
    "dashscope/qwen3.7-max-2026-06-08",
    "moonshot/kimi-k2.7-code",
    "moonshot/kimi-k2.6",
)

_RECOMMENDED_MODEL_NAME_SET = frozenset(name.lower() for name in RECOMMENDED_MODEL_NAMES)

FRONTIER_MODEL_FAMILIES = (
    (("azure", "azure_ai", "bedrock_mantle", "openai"), ("gpt-5",)),
    (
        ("anthropic", "azure_ai", "bedrock", "claude", "databricks", "snowflake", "vertex_ai"),
        ("claude-fable-5", "claude-opus-4", "claude-sonnet-5", "claude-sonnet-4"),
    ),
    (("google", "gemini", "vertex_ai"), ("gemini-3",)),
    (("deepseek",), ("deepseek-v4", "deepseek-r1", "deepseek-reasoner")),
    (("alibaba", "dashscope", "qwen"), ("qwen3.7", "qwen3.5", "qwen3-max")),
    (("moonshot", "moonshotai", "kimi"), ("kimi-k2.7", "kimi-k2.6", "kimi-k2.5")),
)


def model_retry_settings_from_config(settings: Settings) -> ModelRetrySettings:
    """Build SDK retry settings from resolved Strix configuration."""
    llm = settings.llm
    return _build_model_retry_settings(
        max_retries=llm.max_retries,
        initial_delay=llm.retry_initial_delay,
        max_delay=llm.retry_max_delay,
        multiplier=llm.retry_multiplier,
    )


def configure_sdk_model_defaults(settings: Settings) -> None:
    """Apply Strix config to SDK-native defaults."""
    llm = settings.llm
    set_tracing_disabled(True)
    _configure_litellm_compatibility()
    _configure_openrouter_attribution(llm.model)
    if llm.api_key:
        set_default_openai_key(llm.api_key, use_for_tracing=False)
        _configure_litellm_default("api_key", llm.api_key)
        _mirror_api_key_to_provider_env(llm.model, llm.api_key)
    if llm.api_base:
        os.environ["OPENAI_BASE_URL"] = llm.api_base
        _configure_litellm_default("api_base", llm.api_base)
        set_default_openai_api("chat_completions")
    else:
        set_default_openai_api("responses")


def _mirror_api_key_to_provider_env(model_name: str | None, api_key: str) -> None:
    if not model_name:
        return
    import litellm

    name = model_name.strip()
    for prefix in ("litellm/", "any-llm/"):
        if name.lower().startswith(prefix):
            name = name[len(prefix) :]
            break
    try:
        report = litellm.validate_environment(model=name.lower())
    except Exception:  # noqa: BLE001
        return
    for env_key in report.get("missing_keys") or []:
        if env_key.endswith("_API_KEY"):
            os.environ.setdefault(env_key, api_key)


def _configure_litellm_compatibility() -> None:
    """Apply LiteLLM compatibility, privacy, and callback settings."""
    import litellm

    litellm.drop_params = True
    litellm.modify_params = True
    litellm.turn_off_message_logging = True
    # Strix uses LiteLLM's success callback to capture provider-reported cost.
    # Disabling streaming logging also disables that callback for streamed calls.
    litellm.disable_streaming_logging = False
    litellm.suppress_debug_info = True

    _register_litellm_cost_callback()


_OPENROUTER_ATTRIBUTION_HEADERS = {
    "HTTP-Referer": "https://strix.ai",
    "X-Title": "Strix",
    "X-OpenRouter-Categories": "cli-agent",
}


def _configure_openrouter_attribution(model_name: str | None) -> None:
    import litellm

    current: object = litellm.headers
    existing: dict[str, str] = current if isinstance(current, dict) else {}
    if not model_name or "openrouter/" not in model_name.strip().lower():
        if any(key in existing for key in _OPENROUTER_ATTRIBUTION_HEADERS):
            remaining = {
                k: v for k, v in existing.items() if k not in _OPENROUTER_ATTRIBUTION_HEADERS
            }
            litellm.headers = remaining or None  # type: ignore[assignment]
        return

    litellm.headers = {**existing, **_OPENROUTER_ATTRIBUTION_HEADERS}  # type: ignore[assignment]


def _register_litellm_cost_callback() -> None:
    import litellm

    from strix.report.state import litellm_cost_callback

    for bucket_name in ("success_callback", "_async_success_callback"):
        bucket = getattr(litellm, bucket_name, None)
        if not isinstance(bucket, list):
            continue
        if litellm_cost_callback in bucket:
            continue
        bucket.append(litellm_cost_callback)


def _configure_litellm_default(name: str, value: str) -> None:
    """Set LiteLLM's module-level defaults without adding a provider wrapper."""
    import litellm

    setattr(litellm, name, value)


def uses_chat_completions_tool_schema(model_name: str, settings: Settings) -> bool:
    """Return whether the resolved SDK route can only receive JSON function tools."""
    model = model_name.strip().lower()
    if "/" in model and not model.startswith("openai/"):
        return True
    if settings.llm.api_base:
        return True
    return not model_supports_reasoning(model_name)


def model_supports_reasoning(model_name: str) -> bool:
    import litellm

    name = model_name.strip().lower()
    for prefix in ("litellm/", "any-llm/", "openai/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    entry = litellm.model_cost.get(name)
    if entry is None and "/" in name:
        entry = litellm.model_cost.get(name.rsplit("/", 1)[1])
    return bool(entry and entry.get("supports_reasoning"))


def is_recommended_or_frontier_model(model_name: str) -> bool:
    """Return whether a model is recommended or in a frontier model family."""
    name = _normalized_model_name(model_name)
    if not name:
        return False
    if name in _RECOMMENDED_MODEL_NAME_SET:
        return True
    provider_name, bare_model_name = _split_model_provider(name)
    return any(
        _matches_frontier_family(provider_name, bare_model_name, provider_markers, prefixes)
        for provider_markers, prefixes in FRONTIER_MODEL_FAMILIES
    )


def _normalized_model_name(model_name: str) -> str:
    name = model_name.strip().lower()
    for prefix in ("litellm/", "any-llm/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return name


def _split_model_provider(model_name: str) -> tuple[str | None, str]:
    if "/" not in model_name:
        return None, model_name
    provider_name, bare_model_name = model_name.rsplit("/", 1)
    return provider_name, bare_model_name


def _matches_frontier_family(
    provider_name: str | None,
    model_name: str,
    provider_markers: tuple[str, ...],
    model_prefixes: tuple[str, ...],
) -> bool:
    if not _matches_model_prefix(model_name, model_prefixes):
        return False
    if provider_name is None:
        return True
    return _contains_provider_marker(
        provider_name, provider_markers, split_compound_names=True
    ) or _contains_provider_marker(model_name, provider_markers)


def _matches_model_prefix(model_name: str, model_prefixes: tuple[str, ...]) -> bool:
    return any(
        candidate.startswith(prefix)
        for candidate in _model_name_candidates(model_name)
        for prefix in model_prefixes
    )


def _model_name_candidates(model_name: str) -> tuple[str, ...]:
    if "." not in model_name:
        return (model_name,)
    suffixes = tuple(
        model_name.split(".", index)[-1] for index in range(1, model_name.count(".") + 1)
    )
    return (model_name, *suffixes)


def _contains_provider_marker(
    value: str, provider_markers: tuple[str, ...], *, split_compound_names: bool = False
) -> bool:
    parts = set(value.replace(".", "/").split("/"))
    if split_compound_names:
        for separator in ("_", "-"):
            parts.update(piece for part in tuple(parts) for piece in part.split(separator))
    return any(marker in parts for marker in provider_markers)


def is_known_openai_bare_model(model_name: str) -> bool:
    import litellm

    name = model_name.strip().lower()
    if not name or "/" in name:
        return False
    entry = litellm.model_cost.get(name)
    return bool(entry and entry.get("litellm_provider") == "openai")

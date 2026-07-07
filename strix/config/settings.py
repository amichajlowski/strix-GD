"""Strix application settings — pydantic-settings powered."""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]

_BASE_CONFIG = SettingsConfigDict(
    case_sensitive=False,
    populate_by_name=True,
    extra="ignore",
)


class LlmSettings(BaseSettings):
    model_config = _BASE_CONFIG

    model: str | None = Field(default=None, alias="STRIX_LLM")
    api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_KEY", "OPENAI_API_KEY"),
    )
    api_base: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "LLM_API_BASE",
            "OPENAI_API_BASE",
            "OPENAI_BASE_URL",
            "LITELLM_BASE_URL",
            "OLLAMA_API_BASE",
        ),
    )
    reasoning_effort: ReasoningEffort = Field(default="high", alias="STRIX_REASONING_EFFORT")
    timeout: int = Field(default=300, alias="LLM_TIMEOUT")
    # Cap on the tokens sent to the model per request. History that would exceed
    # this is trimmed (oldest turns first) before the call, so an agent's growing
    # session never trips the provider's hard context-length 400. Default 256K.
    # Set to 0 (or less) to disable trimming entirely. When the provider rejects a
    # request with its true limit, that limit is learned and applied automatically
    # (see strix.core.context_limit), so a value set too high self-corrects.
    context_window: int = Field(default=262144, alias="STRIX_LLM_CONTEXT_WINDOW")
    # Headroom kept below the window for output tokens + estimate drift, as a
    # fraction of the window (floored at 16_384 tokens for small windows). See
    # strix.core.context_limit.ContextLimitFilter._budget.
    reserve_ratio: float = Field(default=0.10, ge=0, lt=1, alias="STRIX_LLM_RESERVE_RATIO")
    # Conservative bytes-per-token divisor used by the dependency-free token
    # estimate (lower = more tokens assumed per byte = safer margin).
    bytes_per_token: float = Field(default=3.5, gt=0, alias="STRIX_LLM_BYTES_PER_TOKEN")
    # Fraction of the window at which an agent's stored session is compacted in
    # place (oldest turns summarised into a pointer index). 0 disables compaction
    # (fall back to outbound trimming only). See strix.core.compaction.
    compaction_trigger_ratio: float = Field(
        default=0.70, ge=0, lt=1, alias="STRIX_LLM_COMPACTION_TRIGGER_RATIO"
    )
    # Number of most-recent history items kept verbatim through a compaction.
    compaction_keep_recent: int = Field(default=12, ge=0, alias="STRIX_LLM_COMPACTION_KEEP_RECENT")
    # Model used to summarise the compacted span. Empty = reuse the run's model.
    summarizer_model: str = Field(default="", alias="STRIX_LLM_SUMMARIZER_MODEL")
    max_retries: int = Field(default=5, ge=0, alias="STRIX_LLM_MAX_RETRIES")
    retry_initial_delay: float = Field(
        default=2.0,
        ge=0,
        alias="STRIX_LLM_RETRY_INITIAL_DELAY",
    )
    retry_max_delay: float = Field(default=90.0, ge=0, alias="STRIX_LLM_RETRY_MAX_DELAY")
    retry_multiplier: float = Field(default=2.0, gt=0, alias="STRIX_LLM_RETRY_MULTIPLIER")
    # Cap on concurrent in-flight LLM requests across the whole scan (root + all
    # children + the QA reflection pass). 0 (or less) = unlimited, i.e. current
    # behaviour. Set to N to bound a resource-limited backend (e.g. a local model
    # served from one machine that can only run N generations at once). No ge=/gt=:
    # any value <= 0 is the "disabled" sentinel handled by _llm_semaphore.
    max_concurrency: int = Field(default=0, alias="STRIX_LLM_MAX_CONCURRENCY")


class RuntimeSettings(BaseSettings):
    model_config = _BASE_CONFIG

    image: str = Field(
        default="ghcr.io/usestrix/strix-sandbox:1.0.0",
        alias="STRIX_IMAGE",
    )
    backend: str = Field(default="docker", alias="STRIX_RUNTIME_BACKEND")
    # Hard cap on a local target's size before we refuse to stream it into the
    # sandbox file-by-file (the SDK copies every file individually, which stalls
    # on large repos). Above this, the user must bind-mount via ``--mount``.
    # Set to 0 (or less) to disable the pre-flight check entirely.
    max_local_copy_mb: int = Field(default=1024, alias="STRIX_MAX_LOCAL_COPY_MB")


class TelemetrySettings(BaseSettings):
    model_config = _BASE_CONFIG

    enabled: bool = Field(default=True, alias="STRIX_TELEMETRY")


class IntegrationSettings(BaseSettings):
    model_config = _BASE_CONFIG

    perplexity_api_key: str | None = Field(default=None, alias="PERPLEXITY_API_KEY")


class Settings(BaseSettings):
    model_config = _BASE_CONFIG

    llm: LlmSettings = Field(default_factory=LlmSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    integrations: IntegrationSettings = Field(default_factory=IntegrationSettings)

"""Regression: mid-stream rate-limit APIErrors (no HTTP status) must retry."""

from __future__ import annotations

from agents.retry import ModelRetryNormalizedError, RetryDecision, RetryPolicyContext

from strix.config.models import _parse_retry_delay, retry_in_stream_rate_limit


def _ctx(error: Exception, status_code: int | None) -> RetryPolicyContext:
    return RetryPolicyContext(
        error=error,
        attempt=1,
        max_retries=5,
        stream=True,
        normalized=ModelRetryNormalizedError(status_code=status_code),
    )


def test_in_stream_rate_limit_no_status_retries_with_hinted_delay() -> None:
    # The exact shape seen in the failed audit: bare APIError, no status code.
    msg = (
        "Rate limit reached for gpt-5.6-sol in organization org-x on tokens per "
        "min (TPM): Limit 500000, Used 443074, Requested 67699. Please try again "
        "in 1.292s."
    )
    decision = retry_in_stream_rate_limit(_ctx(RuntimeError(msg), status_code=None))
    assert isinstance(decision, RetryDecision)
    assert decision.retry is True
    assert decision.delay == 1.292


def test_ms_delay_is_converted_to_seconds() -> None:
    msg = "Rate limit reached ... tokens per min ... Please try again in 560ms."
    decision = retry_in_stream_rate_limit(_ctx(RuntimeError(msg), status_code=None))
    assert isinstance(decision, RetryDecision)
    assert decision.delay == 0.560


def test_error_with_http_status_is_left_to_other_policies() -> None:
    # A 429 with a real status is handled by http_status(); don't double-claim it.
    msg = "Rate limit reached ... tokens per min ..."
    assert retry_in_stream_rate_limit(_ctx(RuntimeError(msg), status_code=429)) is False


def test_unrelated_in_stream_error_is_not_retried() -> None:
    # e.g. the cybersecurity content flag — must NOT be retried.
    msg = "This content was flagged for possible cybersecurity risk."
    assert retry_in_stream_rate_limit(_ctx(RuntimeError(msg), status_code=None)) is False


def test_parse_delay_missing_returns_none() -> None:
    assert _parse_retry_delay("Rate limit reached, no hint here") is None

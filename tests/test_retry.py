"""
Tests for retry logic in app/services/fetcher.py.

The core invariant: not all errors are equal.

  retryable=True  (transient) → fetch_page() retries up to max_retry_attempts.
  retryable=False (permanent) → fetch_page() raises immediately, 1 attempt only.

Every AuditError subclass carries this flag; the retry loop reads it rather
than inspecting HTTP status codes directly.  These tests verify that contract
from multiple angles:

  1. Each transient error type triggers a retry that succeeds.
  2. Exhausting all retries raises the last exception.
  3. Each permanent error type is raised after exactly 1 attempt.
  4. The `attempts` field on FetchResult accurately reflects what happened.
  5. asyncio.sleep is called with the configured backoff (not just called once).
  6. The retryable flag on each exception class matches the documented contract.
"""

import asyncio
from unittest.mock import AsyncMock, patch, call

import pytest

from app.core.exceptions import (
    ContentTooLargeError,
    FetchTimeoutError,
    TooManyRedirectsError,
    TransientUpstreamError,
    UnreachableURLError,
    UpstreamHTTPError,
    UnsupportedContentTypeError,
)
from app.services.fetcher import FetchResult, fetch_page

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GOOD_RESULT = FetchResult(
    final_url="https://example.com/",
    http_status=200,
    response_time_ms=50,
    html="<html><body>ok</body></html>",
    attempts=1,
)


def _make_attempt_mock(*side_effects):
    """
    Return an async callable for _attempt_fetch that yields each item in
    `side_effects` in order — raising exceptions, returning non-exceptions.
    """

    async def _mock(url: str):
        effect = side_effects[_mock.call_count]
        _mock.call_count += 1
        if isinstance(effect, BaseException):
            raise effect
        return effect

    _mock.call_count = 0
    return _mock


# ---------------------------------------------------------------------------
# Exception class contracts — retryable flags
# ---------------------------------------------------------------------------


class TestRetryableFlags:
    """The retryable flag on each exception class is the source of truth.
    These tests make that contract explicit and machine-verifiable."""

    def test_fetch_timeout_is_retryable(self):
        assert FetchTimeoutError.retryable is True

    def test_unreachable_url_is_retryable(self):
        assert UnreachableURLError.retryable is True

    def test_transient_upstream_error_is_retryable(self):
        assert TransientUpstreamError.retryable is True

    def test_upstream_http_error_is_not_retryable(self):
        assert UpstreamHTTPError.retryable is False

    def test_unsupported_content_type_is_not_retryable(self):
        assert UnsupportedContentTypeError.retryable is False

    def test_content_too_large_is_not_retryable(self):
        assert ContentTooLargeError.retryable is False

    def test_too_many_redirects_is_not_retryable(self):
        assert TooManyRedirectsError.retryable is False


# ---------------------------------------------------------------------------
# Transient failures → retry → success
# ---------------------------------------------------------------------------


class TestTransientRetry:
    @pytest.mark.asyncio
    @patch("app.services.fetcher.asyncio.sleep", new_callable=AsyncMock)
    async def test_retries_on_timeout(self, mock_sleep):
        mock = _make_attempt_mock(FetchTimeoutError("timed out"), _GOOD_RESULT)
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            result = await fetch_page("https://example.com/")
        assert result.http_status == 200
        assert mock.call_count == 2
        mock_sleep.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("app.services.fetcher.asyncio.sleep", new_callable=AsyncMock)
    async def test_retries_on_connection_error(self, mock_sleep):
        mock = _make_attempt_mock(UnreachableURLError("conn reset"), _GOOD_RESULT)
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            result = await fetch_page("https://example.com/")
        assert result.http_status == 200
        assert mock.call_count == 2

    @pytest.mark.asyncio
    @patch("app.services.fetcher.asyncio.sleep", new_callable=AsyncMock)
    async def test_retries_on_503(self, mock_sleep):
        mock = _make_attempt_mock(
            TransientUpstreamError("503 overloaded", upstream_status=503),
            _GOOD_RESULT,
        )
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            result = await fetch_page("https://example.com/")
        assert result.http_status == 200
        assert mock.call_count == 2

    @pytest.mark.asyncio
    @patch("app.services.fetcher.asyncio.sleep", new_callable=AsyncMock)
    async def test_attempts_field_reflects_retry_count(self, mock_sleep):
        """After one retry, attempts must equal 2."""
        mock = _make_attempt_mock(FetchTimeoutError("slow"), _GOOD_RESULT)
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            result = await fetch_page("https://example.com/")
        assert result.attempts == 2

    @pytest.mark.asyncio
    @patch("app.services.fetcher.asyncio.sleep", new_callable=AsyncMock)
    async def test_backoff_called_with_configured_delay(self, mock_sleep):
        """asyncio.sleep must be called with retry_backoff_seconds, not an arbitrary value."""
        from app.core.config import settings

        mock = _make_attempt_mock(FetchTimeoutError("slow"), _GOOD_RESULT)
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            await fetch_page("https://example.com/")
        mock_sleep.assert_awaited_once_with(settings.retry_backoff_seconds)


# ---------------------------------------------------------------------------
# Exhausting retries
# ---------------------------------------------------------------------------


class TestRetryExhaustion:
    @pytest.mark.asyncio
    @patch("app.services.fetcher.asyncio.sleep", new_callable=AsyncMock)
    async def test_raises_after_all_attempts_fail(self, mock_sleep):
        mock = _make_attempt_mock(
            FetchTimeoutError("timeout #1"),
            FetchTimeoutError("timeout #2"),
        )
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            with pytest.raises(FetchTimeoutError):
                await fetch_page("https://slow.example.com/")
        assert mock.call_count == 2

    @pytest.mark.asyncio
    @patch("app.services.fetcher.asyncio.sleep", new_callable=AsyncMock)
    async def test_raises_last_exception_not_first(self, mock_sleep):
        """The exception raised after exhaustion should be the last one."""
        first = FetchTimeoutError("first timeout")
        second = FetchTimeoutError("second timeout — this is the one that propagates")
        mock = _make_attempt_mock(first, second)
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            with pytest.raises(FetchTimeoutError) as exc_info:
                await fetch_page("https://slow.example.com/")
        assert "second" in exc_info.value.message

    @pytest.mark.asyncio
    @patch("app.services.fetcher.asyncio.sleep", new_callable=AsyncMock)
    async def test_sleep_called_once_for_single_retry(self, mock_sleep):
        """With max_retry_attempts=2 (1 retry), sleep must be called exactly once."""
        mock = _make_attempt_mock(
            FetchTimeoutError("timeout"),
            FetchTimeoutError("timeout again"),
        )
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            with pytest.raises(FetchTimeoutError):
                await fetch_page("https://slow.example.com/")
        assert mock_sleep.await_count == 1


# ---------------------------------------------------------------------------
# Permanent failures — never retried
# ---------------------------------------------------------------------------


class TestPermanentFailures:
    @pytest.mark.asyncio
    async def test_404_raises_immediately(self):
        mock = _make_attempt_mock(UpstreamHTTPError("404", upstream_status=404))
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            with pytest.raises(UpstreamHTTPError):
                await fetch_page("https://example.com/missing")
        assert mock.call_count == 1

    @pytest.mark.asyncio
    async def test_403_raises_immediately(self):
        mock = _make_attempt_mock(UpstreamHTTPError("403", upstream_status=403))
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            with pytest.raises(UpstreamHTTPError):
                await fetch_page("https://example.com/forbidden")
        assert mock.call_count == 1

    @pytest.mark.asyncio
    async def test_410_raises_immediately(self):
        mock = _make_attempt_mock(UpstreamHTTPError("410 gone", upstream_status=410))
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            with pytest.raises(UpstreamHTTPError):
                await fetch_page("https://example.com/gone")
        assert mock.call_count == 1

    @pytest.mark.asyncio
    async def test_wrong_content_type_raises_immediately(self):
        mock = _make_attempt_mock(UnsupportedContentTypeError("application/json"))
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            with pytest.raises(UnsupportedContentTypeError):
                await fetch_page("https://api.example.com/data.json")
        assert mock.call_count == 1

    @pytest.mark.asyncio
    async def test_too_many_redirects_raises_immediately(self):
        mock = _make_attempt_mock(TooManyRedirectsError("redirect loop"))
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            with pytest.raises(TooManyRedirectsError):
                await fetch_page("https://looping.example.com/")
        assert mock.call_count == 1

    @pytest.mark.asyncio
    async def test_content_too_large_raises_immediately(self):
        mock = _make_attempt_mock(ContentTooLargeError("exceeded 5MB"))
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            with pytest.raises(ContentTooLargeError):
                await fetch_page("https://huge.example.com/")
        assert mock.call_count == 1

    @pytest.mark.asyncio
    async def test_permanent_failure_never_calls_sleep(self):
        mock = _make_attempt_mock(UpstreamHTTPError("404", upstream_status=404))
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            with patch(
                "app.services.fetcher.asyncio.sleep", new_callable=AsyncMock
            ) as mock_sleep:
                with pytest.raises(UpstreamHTTPError):
                    await fetch_page("https://example.com/missing")
        mock_sleep.assert_not_awaited()


# ---------------------------------------------------------------------------
# Clean success — no retry needed
# ---------------------------------------------------------------------------


class TestCleanSuccess:
    @pytest.mark.asyncio
    async def test_attempts_is_1_on_first_success(self):
        mock = _make_attempt_mock(_GOOD_RESULT)
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            result = await fetch_page("https://example.com/")
        assert result.attempts == 1

    @pytest.mark.asyncio
    async def test_no_sleep_on_first_success(self):
        mock = _make_attempt_mock(_GOOD_RESULT)
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            with patch(
                "app.services.fetcher.asyncio.sleep", new_callable=AsyncMock
            ) as mock_sleep:
                await fetch_page("https://example.com/")
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_result_fields_pass_through_unchanged(self):
        mock = _make_attempt_mock(_GOOD_RESULT)
        with patch("app.services.fetcher._attempt_fetch", side_effect=mock):
            result = await fetch_page("https://example.com/")
        assert result.final_url == "https://example.com/"
        assert result.http_status == 200
        assert result.html == "<html><body>ok</body></html>"

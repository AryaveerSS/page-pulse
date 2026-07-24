"""
Tests for the retry logic in app/services/fetcher.py.

Core invariant being tested:
  - Transient failures (timeout, connection error, 503) are retried.
  - Permanent failures (404, 410, wrong content-type) are raised immediately
    with exactly 1 attempt — never retried.

The tests mock _attempt_fetch directly so they don't touch the network,
and they patch settings.max_retry_attempts to keep execution fast
(no real asyncio.sleep waits).
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.core.exceptions import (
    FetchTimeoutError,
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
    Return an AsyncMock for _attempt_fetch that raises/returns each item in
    `side_effects` in order.  Exceptions are raised; non-exceptions returned.
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
# Transient failure → retry → success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("app.services.fetcher.asyncio.sleep", new_callable=AsyncMock)
async def test_retries_on_timeout_then_succeeds(mock_sleep):
    """A single timeout should trigger one retry that succeeds."""
    attempt_mock = _make_attempt_mock(
        FetchTimeoutError("timed out"),
        _GOOD_RESULT,
    )

    with patch("app.services.fetcher._attempt_fetch", side_effect=attempt_mock):
        result = await fetch_page("https://example.com/")

    assert result.http_status == 200
    assert attempt_mock.call_count == 2
    mock_sleep.assert_awaited_once()


@pytest.mark.asyncio
@patch("app.services.fetcher.asyncio.sleep", new_callable=AsyncMock)
async def test_retries_on_connection_error_then_succeeds(mock_sleep):
    """A connection error (transient) should trigger a retry."""
    attempt_mock = _make_attempt_mock(
        UnreachableURLError("conn reset"),
        _GOOD_RESULT,
    )

    with patch("app.services.fetcher._attempt_fetch", side_effect=attempt_mock):
        result = await fetch_page("https://example.com/")

    assert result.http_status == 200
    assert attempt_mock.call_count == 2
    mock_sleep.assert_awaited_once()


@pytest.mark.asyncio
@patch("app.services.fetcher.asyncio.sleep", new_callable=AsyncMock)
async def test_retries_on_503_then_succeeds(mock_sleep):
    """A 503 (TransientUpstreamError) should be retried."""
    attempt_mock = _make_attempt_mock(
        TransientUpstreamError("503 overloaded", upstream_status=503),
        _GOOD_RESULT,
    )

    with patch("app.services.fetcher._attempt_fetch", side_effect=attempt_mock):
        result = await fetch_page("https://example.com/")

    assert result.http_status == 200
    assert attempt_mock.call_count == 2


# ---------------------------------------------------------------------------
# Transient failure — exhausts all retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("app.services.fetcher.asyncio.sleep", new_callable=AsyncMock)
async def test_raises_after_exhausting_retries(mock_sleep):
    """If every attempt fails transiently, the last exception is raised."""
    attempt_mock = _make_attempt_mock(
        FetchTimeoutError("timeout #1"),
        FetchTimeoutError("timeout #2"),
    )

    with patch("app.services.fetcher._attempt_fetch", side_effect=attempt_mock):
        with pytest.raises(FetchTimeoutError):
            await fetch_page("https://slow.example.com/")

    assert attempt_mock.call_count == 2  # used all retries


# ---------------------------------------------------------------------------
# Permanent failures — never retried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_is_not_retried():
    """A 404 (UpstreamHTTPError, retryable=False) must never be retried."""
    attempt_mock = _make_attempt_mock(
        UpstreamHTTPError("404 not found", upstream_status=404),
    )

    with patch("app.services.fetcher._attempt_fetch", side_effect=attempt_mock):
        with pytest.raises(UpstreamHTTPError):
            await fetch_page("https://example.com/missing")

    # Exactly one attempt — retry loop must bail immediately.
    assert attempt_mock.call_count == 1


@pytest.mark.asyncio
async def test_wrong_content_type_is_not_retried():
    """UnsupportedContentTypeError (retryable=False) is raised immediately."""
    attempt_mock = _make_attempt_mock(
        UnsupportedContentTypeError("got application/json"),
    )

    with patch("app.services.fetcher._attempt_fetch", side_effect=attempt_mock):
        with pytest.raises(UnsupportedContentTypeError):
            await fetch_page("https://api.example.com/data.json")

    assert attempt_mock.call_count == 1


@pytest.mark.asyncio
async def test_410_gone_is_not_retried():
    """HTTP 410 Gone is a permanent client error and must never be retried."""
    attempt_mock = _make_attempt_mock(
        UpstreamHTTPError("410 gone", upstream_status=410),
    )

    with patch("app.services.fetcher._attempt_fetch", side_effect=attempt_mock):
        with pytest.raises(UpstreamHTTPError):
            await fetch_page("https://example.com/gone")

    assert attempt_mock.call_count == 1


# ---------------------------------------------------------------------------
# Happy path — no retry needed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_on_first_attempt_sets_attempts_to_1():
    """A clean success should report attempts=1 and never call sleep."""
    attempt_mock = _make_attempt_mock(_GOOD_RESULT)

    with patch("app.services.fetcher._attempt_fetch", side_effect=attempt_mock):
        with patch(
            "app.services.fetcher.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            result = await fetch_page("https://example.com/")

    assert result.attempts == 1
    mock_sleep.assert_not_awaited()

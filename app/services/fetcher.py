
import asyncio
import time

import httpx

from app.core.config import settings
from app.core.exceptions import (
    AuditError,
    ContentTooLargeError,
    FetchTimeoutError,
    TooManyRedirectsError,
    TransientUpstreamError,
    UnreachableURLError,
    UnsupportedContentTypeError,
    UpstreamHTTPError,
)

HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")

# HTTP 5xx codes we treat as transient (server overloaded / temporarily down).
# Everything else in the 5xx range is treated as permanent to avoid hammering
# a misconfigured server or triggering WAF bans.
_TRANSIENT_5XX = {429, 500, 502, 503, 504}


class FetchResult:
    __slots__ = ("final_url", "http_status", "response_time_ms", "html", "attempts")

    def __init__(
        self,
        final_url: str,
        http_status: int,
        response_time_ms: int,
        html: str,
        attempts: int = 1,
    ):
        self.final_url = final_url
        self.http_status = http_status
        self.response_time_ms = response_time_ms
        self.html = html
        self.attempts = attempts


async def _attempt_fetch(url: str) -> FetchResult:
    """
    Single fetch attempt — no retry logic here.  Raises a typed AuditError
    for every anticipated failure mode so the caller can decide whether to
    retry based on `exc.retryable`.
    """
    headers = {"User-Agent": settings.user_agent}
    timeout = httpx.Timeout(settings.fetch_timeout_seconds)

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=settings.max_redirects,
            headers=headers,
        ) as client:
            async with client.stream("GET", url) as response:
                content_type = (
                    response.headers.get("content-type", "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )

                if response.status_code >= 400:
                    if response.status_code in _TRANSIENT_5XX:
                        # Transient server-side overload — safe to retry.
                        raise TransientUpstreamError(
                            f"Target responded with HTTP {response.status_code} (transient).",
                            upstream_status=response.status_code,
                        )
                    # 4xx and non-transient 5xx are permanent failures.
                    raise UpstreamHTTPError(
                        f"Target responded with HTTP {response.status_code}.",
                        upstream_status=response.status_code,
                    )

                if content_type and content_type not in HTML_CONTENT_TYPES:
                    raise UnsupportedContentTypeError(
                        f"Expected an HTML page but got content-type '{content_type}'."
                    )

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > settings.max_content_bytes:
                        raise ContentTooLargeError(
                            f"Page exceeded the {settings.max_content_bytes // (1024 * 1024)}MB"
                            " size limit."
                        )
                    chunks.append(chunk)

                elapsed_ms = int((time.perf_counter() - start) * 1000)
                html = b"".join(chunks).decode(
                    response.encoding or "utf-8", errors="replace"
                )

                return FetchResult(
                    final_url=str(response.url),
                    http_status=response.status_code,
                    response_time_ms=elapsed_ms,
                    html=html,
                )

    except httpx.TooManyRedirects as exc:
        raise TooManyRedirectsError(
            "Too many redirects while following this URL."
        ) from exc
    except httpx.TimeoutException as exc:
        raise FetchTimeoutError(
            f"Target did not respond within {settings.fetch_timeout_seconds}s."
        ) from exc
    except httpx.ConnectError as exc:
        raise UnreachableURLError(
            "Could not connect to this host (DNS or connection failure)."
        ) from exc
    except httpx.HTTPError as exc:
        raise UnreachableURLError(
            f"Network error while fetching the URL: {exc}"
        ) from exc


async def fetch_page(url: str) -> FetchResult:
    """
    Fetch `url` with automatic retry for transient failures.

    Retries up to `settings.max_retry_attempts - 1` additional times
    (default: 1 retry = 2 total attempts) for errors flagged `retryable`.
    Permanent errors are re-raised immediately without any retry.
    """
    last_exc: AuditError | None = None

    for attempt in range(1, settings.max_retry_attempts + 1):
        try:
            result = await _attempt_fetch(url)
            result.attempts = attempt
            return result
        except AuditError as exc:
            last_exc = exc
            if not exc.retryable or attempt == settings.max_retry_attempts:
                # Permanent failure, or we've exhausted our retries.
                raise
            # Transient failure — wait briefly, then try again.
            await asyncio.sleep(settings.retry_backoff_seconds)

    # Unreachable, but satisfies the type checker.
    raise last_exc  # type: ignore[misc]

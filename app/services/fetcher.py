"""
Handles the network side of an audit: fetching a URL safely.

Kept deliberately separate from parsing (see services/parser.py) so each can
be tested independently - fetcher.py is the only module that touches the
network, parser.py is pure functions over already-downloaded text. That
split is design decision #1 in the README.
"""
import time

import httpx

from app.core.config import settings
from app.core.exceptions import (
    ContentTooLargeError,
    FetchTimeoutError,
    TooManyRedirectsError,
    UnreachableURLError,
    UnsupportedContentTypeError,
    UpstreamHTTPError,
)

HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


class FetchResult:
    __slots__ = ("final_url", "http_status", "response_time_ms", "html")

    def __init__(self, final_url: str, http_status: int, response_time_ms: int, html: str):
        self.final_url = final_url
        self.http_status = http_status
        self.response_time_ms = response_time_ms
        self.html = html


async def fetch_page(url: str) -> FetchResult:
    """
    Fetch `url` and return its HTML plus timing/status metadata.

    Raises a specific AuditError subclass for every anticipated failure mode
    instead of letting httpx exceptions leak past this module.
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
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if response.status_code >= 400:
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
                            f"Page exceeded the {settings.max_content_bytes // (1024*1024)}MB size limit."
                        )
                    chunks.append(chunk)

                elapsed_ms = int((time.perf_counter() - start) * 1000)
                html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")

                return FetchResult(
                    final_url=str(response.url),
                    http_status=response.status_code,
                    response_time_ms=elapsed_ms,
                    html=html,
                )

    except httpx.TooManyRedirects as exc:
        raise TooManyRedirectsError("Too many redirects while following this URL.") from exc
    except httpx.TimeoutException as exc:
        raise FetchTimeoutError(
            f"Target did not respond within {settings.fetch_timeout_seconds}s."
        ) from exc
    except httpx.ConnectError as exc:
        raise UnreachableURLError("Could not connect to this host (DNS or connection failure).") from exc
    except httpx.HTTPError as exc:
        raise UnreachableURLError(f"Network error while fetching the URL: {exc}") from exc

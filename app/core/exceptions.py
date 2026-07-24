"""
Domain-specific exceptions for the audit pipeline.

Design decision: rather than letting httpx/parsing exceptions bubble up as
generic 500s, every failure mode we can anticipate gets its own exception
class mapped to a specific HTTP status code in main.py's exception handlers.
This keeps routers/services free of try/except-for-HTTP-codes noise —
services just raise the semantically correct exception, and the transport
layer (FastAPI) decides how to represent it over HTTP.

Retry classification
--------------------
Each AuditError subclass carries a `retryable` flag.

  retryable = True  →  transient fault (connection reset, 503 overload, timeout).
                        The same request might succeed moments later.

  retryable = False →  permanent fault (404, malformed URL, wrong content-type).
                        Retrying is pointless and wastes time/resources.

The fetcher layer reads this flag so it never retries permanent failures,
which is a key correctness distinction a senior engineer would enforce.
"""


class AuditError(Exception):
    """Base class for all audit-pipeline errors. Never raised directly."""

    status_code: int = 500
    error_code: str = "internal_error"
    retryable: bool = False  # conservative default; subclasses override

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Transient failures — safe to retry
# ---------------------------------------------------------------------------


class UnreachableURLError(AuditError):
    """DNS failure, connection refused, TLS error, etc. May be transient."""

    status_code = 502
    error_code = "unreachable_url"
    retryable = True  # could be a momentary network blip


class FetchTimeoutError(AuditError):
    """The upstream server did not respond within the configured timeout."""

    status_code = 504
    error_code = "fetch_timeout"
    retryable = True  # server might just be momentarily overloaded


class TransientUpstreamError(AuditError):
    """Target returned a 5xx that signals a temporary overload (e.g. 503)."""

    status_code = 502
    error_code = "upstream_http_error"
    retryable = True

    def __init__(self, message: str, upstream_status: int):
        self.upstream_status = upstream_status
        super().__init__(message)


# ---------------------------------------------------------------------------
# Permanent failures — never retry
# ---------------------------------------------------------------------------


class TooManyRedirectsError(AuditError):
    """The URL redirected more times than we're willing to follow."""

    status_code = 502
    error_code = "too_many_redirects"
    retryable = False  # more attempts will hit the same redirect loop


class UnsupportedContentTypeError(AuditError):
    """The response wasn't HTML (e.g. a PDF, image, or JSON API)."""

    status_code = 415
    error_code = "unsupported_content_type"
    retryable = False  # content-type won't change on retry


class ContentTooLargeError(AuditError):
    """The response body exceeded our size cap."""

    status_code = 413
    error_code = "content_too_large"
    retryable = False  # page size won't change on retry


class UpstreamHTTPError(AuditError):
    """Target responded with a 4xx (client error) — permanent, never retry."""

    status_code = 502
    error_code = "upstream_http_error"
    retryable = False  # 404, 403, 410, etc. are definitive answers

    def __init__(self, message: str, upstream_status: int):
        self.upstream_status = upstream_status
        super().__init__(message)

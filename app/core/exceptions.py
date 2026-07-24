"""
Domain-specific exceptions for the audit pipeline.

Design decision: rather than letting httpx/parsing exceptions bubble up as
generic 500s, every failure mode we can anticipate gets its own exception
class mapped to a specific HTTP status code in main.py's exception handlers.
This keeps routers/services free of try/except-for-HTTP-codes noise -
services just raise the semantically correct exception, and the transport
layer (FastAPI) decides how to represent it over HTTP.
"""


class AuditError(Exception):
    """Base class for all audit-pipeline errors. Never raised directly."""

    status_code: int = 500
    error_code: str = "internal_error"

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class UnreachableURLError(AuditError):
    """DNS failure, connection refused, TLS error, etc. Upstream is unreachable."""

    status_code = 502
    error_code = "unreachable_url"


class FetchTimeoutError(AuditError):
    """The upstream server did not respond within the configured timeout."""

    status_code = 504
    error_code = "fetch_timeout"


class TooManyRedirectsError(AuditError):
    """The URL redirected more times than we're willing to follow."""

    status_code = 502
    error_code = "too_many_redirects"


class UnsupportedContentTypeError(AuditError):
    """The response wasn't HTML (e.g. a PDF, image, or JSON API)."""

    status_code = 415
    error_code = "unsupported_content_type"


class ContentTooLargeError(AuditError):
    """The response body exceeded our size cap."""

    status_code = 413
    error_code = "content_too_large"


class UpstreamHTTPError(AuditError):
    """The target URL responded, but with a 4xx/5xx we're surfacing as-is."""

    status_code = 502
    error_code = "upstream_http_error"

    def __init__(self, message: str, upstream_status: int):
        self.upstream_status = upstream_status
        super().__init__(message)

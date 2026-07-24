"""
Shared audit orchestration layer.

Wraps fetch + parse into a single async function used by both the single-URL
endpoint and the batch endpoint.  Centralising this logic means the two
routes stay thin and don't drift apart in behaviour.
"""

from app.schemas.audit import AuditReport
from app.services.fetcher import fetch_page
from app.services.parser import parse_html


async def run_audit(url: str) -> AuditReport:
    """
    Fetch `url`, parse its HTML, and return a populated AuditReport.

    Raises a typed AuditError subclass on any network or content failure;
    callers (routers, batch gatherers) decide how to surface those.
    """
    result = await fetch_page(url)
    parsed = parse_html(result.html)

    return AuditReport(
        url=result.final_url,
        requested_url=url,
        http_status=result.http_status,
        response_time_ms=result.response_time_ms,
        attempts=result.attempts,
        **parsed,
    )

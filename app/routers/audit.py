from fastapi import APIRouter

from app.schemas.audit import AuditReport, AuditRequest, ErrorResponse
from app.services.fetcher import fetch_page
from app.services.parser import parse_html

router = APIRouter(prefix="/api", tags=["audit"])

# Each entry documents exactly which of our exception classes maps to which
# status code, so /docs shows a reviewer every failure mode up front instead
# of just the happy path.
_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Malformed request."},
    413: {"model": ErrorResponse, "description": "Target page exceeded the size limit."},
    415: {"model": ErrorResponse, "description": "Target response was not HTML."},
    422: {"model": ErrorResponse, "description": "URL failed validation."},
    502: {"model": ErrorResponse, "description": "Target host unreachable or returned an error."},
    504: {"model": ErrorResponse, "description": "Target host timed out."},
}


@router.post(
    "/audit",
    response_model=AuditReport,
    responses=_ERROR_RESPONSES,
    summary="Audit a URL",
    description=(
        "Fetches the given URL and returns page-health metrics: HTTP status, "
        "response time, title, meta description, heading structure, image "
        "alt-text coverage, and approximate word count."
    ),
)
async def audit_url(payload: AuditRequest) -> AuditReport:
    result = await fetch_page(str(payload.url))
    parsed = parse_html(result.html)

    return AuditReport(
        url=result.final_url,
        requested_url=str(payload.url),
        http_status=result.http_status,
        response_time_ms=result.response_time_ms,
        **parsed,
    )


@router.get("/health", summary="Liveness check")
async def health() -> dict:
    return {"status": "ok"}

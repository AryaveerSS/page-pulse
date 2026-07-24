"""
Audit router — exposes:

  POST /api/audit        — single-URL audit
  POST /api/audit/batch  — parallel multi-URL audit
  GET  /api/health       — liveness probe

Rate limiting
-------------
Both audit endpoints are limited to `settings.rate_limit` requests per IP
(default: 30/minute).  The /health endpoint is exempt — monitoring systems
should never be rate-limited.

Batch design
------------
asyncio.gather(*tasks, return_exceptions=True) runs all URL fetches in
parallel and collects results without short-circuiting on the first failure.
Each URL gets its own success/failure entry in the response, so a single bad
URL never spoils the rest of the batch.
"""

import asyncio

from fastapi import APIRouter, Request

from app.core.config import settings
from app.core.exceptions import AuditError
from app.core.limiter import limiter
from app.schemas.audit import (
    AuditReport,
    AuditRequest,
    BatchAuditItem,
    BatchAuditRequest,
    BatchAuditResponse,
    ErrorResponse,
)
from app.services.audit_service import run_audit

router = APIRouter(prefix="/api", tags=["audit"])

# Each entry documents exactly which of our exception classes maps to which
# status code, so /docs shows a reviewer every failure mode up front instead
# of just the happy path.
_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Malformed request."},
    413: {
        "model": ErrorResponse,
        "description": "Target page exceeded the size limit.",
    },
    415: {"model": ErrorResponse, "description": "Target response was not HTML."},
    422: {"model": ErrorResponse, "description": "URL failed validation."},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
    502: {
        "model": ErrorResponse,
        "description": "Target host unreachable or returned an error.",
    },
    504: {"model": ErrorResponse, "description": "Target host timed out."},
}


@router.post(
    "/audit",
    response_model=AuditReport,
    responses=_ERROR_RESPONSES,
    summary="Audit a single URL",
    description=(
        "Fetches the given URL and returns page-health metrics: HTTP status, "
        "response time, title, meta description, heading structure, image "
        "alt-text coverage, and approximate word count.  Transient network "
        "failures are retried automatically; permanent failures (404, wrong "
        "content-type) are surfaced immediately."
    ),
)
@limiter.limit(settings.rate_limit)
async def audit_url(request: Request, payload: AuditRequest) -> AuditReport:
    return await run_audit(str(payload.url))


@router.post(
    "/audit/batch",
    response_model=BatchAuditResponse,
    responses=_ERROR_RESPONSES,
    summary="Audit multiple URLs in parallel",
    description=(
        f"Accepts 1–{settings.batch_max_urls} URLs and audits them concurrently "
        "using asyncio.gather.  Each URL is audited independently: one failure "
        "never causes others to be skipped.  The response contains a per-URL "
        "result with its own success/error state."
    ),
)
@limiter.limit(settings.rate_limit)
async def audit_batch(
    request: Request, payload: BatchAuditRequest
) -> BatchAuditResponse:
    urls = [str(u) for u in payload.urls]

    # Run all audits concurrently; return_exceptions=True means a failure in
    # one coroutine is returned as an exception object rather than propagated,
    # so the other audits continue to completion.
    tasks = [run_audit(url) for url in urls]
    raw_results: list[AuditReport | BaseException] = await asyncio.gather(
        *tasks, return_exceptions=True
    )

    results: list[BatchAuditItem] = []
    for url, outcome in zip(urls, raw_results):
        if isinstance(outcome, AuditError):
            results.append(
                BatchAuditItem(
                    url=url,
                    success=False,
                    error_code=outcome.error_code,
                    error_message=outcome.message,
                )
            )
        elif isinstance(outcome, BaseException):
            # Unexpected error — surface it without crashing the whole batch.
            results.append(
                BatchAuditItem(
                    url=url,
                    success=False,
                    error_code="internal_error",
                    error_message=str(outcome),
                )
            )
        else:
            results.append(BatchAuditItem(url=url, success=True, report=outcome))

    succeeded = sum(1 for r in results if r.success)
    return BatchAuditResponse(
        total=len(urls),
        succeeded=succeeded,
        failed=len(urls) - succeeded,
        results=results,
    )


@router.get("/health", summary="Liveness check")
async def health() -> dict:
    return {"status": "ok"}

"""
Request/response contracts for the /api/audit and /api/audit/batch endpoints.

Using Pydantic models (rather than raw dicts) means:
  - Invalid input is rejected with a 422 and a precise error body before any
    of our code runs — we never see a malformed URL in business logic.
  - The response shape is guaranteed and self-documenting via /docs.
  - Field descriptions here become the OpenAPI schema shown to consumers.
"""

from datetime import datetime, timezone
from typing import Annotated

from pydantic import AnyHttpUrl, BaseModel, Field

from app.core.config import settings


class AuditRequest(BaseModel):
    url: AnyHttpUrl = Field(
        ...,
        description="The absolute http(s) URL to audit.",
        examples=["https://example.com"],
    )


class AuditReport(BaseModel):
    url: str = Field(
        ..., description="The final URL that was audited (after redirects)."
    )
    requested_url: str = Field(..., description="The URL exactly as submitted.")
    http_status: int = Field(
        ..., description="HTTP status code returned by the target page."
    )
    response_time_ms: int = Field(
        ..., description="Time to fetch the page, in milliseconds."
    )
    attempts: int = Field(
        default=1,
        description=(
            "Number of fetch attempts made. Greater than 1 means a transient "
            "failure was encountered and a retry succeeded."
        ),
    )
    title: str | None = Field(
        None, description="Contents of the <title> tag, if present."
    )
    meta_description: str | None = Field(
        None, description="Contents of <meta name='description'>, if present."
    )
    h1_count: int = Field(..., description="Number of <h1> elements found.")
    image_count: int = Field(..., description="Total number of <img> elements found.")
    images_missing_alt: int = Field(
        ...,
        description="Number of <img> elements with no alt attribute, or an empty one.",
    )
    word_count: int = Field(
        ..., description="Approximate word count of the visible page text."
    )
    audited_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ErrorResponse(BaseModel):
    """
    The single error shape returned by every failure mode in this API.
    A consumer only ever needs to handle one error schema, regardless of
    which of the many things that can go wrong actually happened.
    """

    error_code: str = Field(..., description="Machine-readable error identifier.")
    message: str = Field(..., description="Human-readable explanation.")


# ---------------------------------------------------------------------------
# Batch endpoint schemas
# ---------------------------------------------------------------------------


class BatchAuditRequest(BaseModel):
    urls: Annotated[
        list[AnyHttpUrl],
        Field(
            min_length=1,
            max_length=settings.batch_max_urls,
            description=(
                f"List of absolute http(s) URLs to audit in parallel "
                f"(1–{settings.batch_max_urls} URLs per request)."
            ),
            examples=[["https://example.com", "https://example.org"]],
        ),
    ]


class BatchAuditItem(BaseModel):
    """Result for a single URL within a batch request."""

    url: str = Field(..., description="The URL that was submitted.")
    success: bool = Field(..., description="Whether the audit completed without error.")
    report: AuditReport | None = Field(
        None, description="The audit report, populated on success."
    )
    error_code: str | None = Field(
        None, description="Machine-readable error code, populated on failure."
    )
    error_message: str | None = Field(
        None, description="Human-readable error message, populated on failure."
    )


class BatchAuditResponse(BaseModel):
    """
    Batch audit response.

    One bad URL never fails the whole batch — each item carries its own
    success/failure state so callers can handle them independently.
    """

    total: int = Field(..., description="Number of URLs submitted.")
    succeeded: int = Field(..., description="Number of URLs audited successfully.")
    failed: int = Field(..., description="Number of URLs that produced an error.")
    results: list[BatchAuditItem] = Field(
        ..., description="Per-URL results, in submission order."
    )
    audited_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

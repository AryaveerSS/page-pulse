"""
App entrypoint: wires up the router, CORS, static frontend, rate limiting,
and the exception → HTTP response mapping.

Design decision: every AuditError subclass is caught by a single handler
here and turned into the same ErrorResponse shape with its own status_code.
Adding a new failure mode later means adding one exception class in
core/exceptions.py — nothing here has to change.

Rate limiting is handled by slowapi.  The limiter singleton lives in
core/limiter.py so the router can import it without a circular dependency.
The 429 response from slowapi is intercepted and reshaped to match our
standard {error_code, message} envelope.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.core.config import settings
from app.core.exceptions import AuditError
from app.core.limiter import limiter
from app.routers import audit

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Audits any URL and reports on-page health metrics.",
)

# Attach the limiter to the app state so slowapi can find it.
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(audit.router)


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


@app.exception_handler(AuditError)
async def audit_error_handler(request: Request, exc: AuditError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error_code": exc.error_code, "message": exc.message},
    )


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    # Reshape slowapi's default response to our standard error envelope.
    return JSONResponse(
        status_code=429,
        content={
            "error_code": "rate_limit_exceeded",
            "message": f"Too many requests. Limit: {settings.rate_limit}.",
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Pydantic gives a rich `errors()` list; we surface a single readable
    # message while keeping the same {error_code, message} envelope as
    # every other error in this API.
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    detail = first.get("msg", "Invalid request.")
    return JSONResponse(
        status_code=422,
        content={
            "error_code": "validation_error",
            "message": f"{field}: {detail}" if field else detail,
        },
    )


# Serve the frontend (static/index.html etc.) at the root, after the API
# routes so /api/* and /docs are never shadowed by the static mount.
app.mount("/", StaticFiles(directory="static", html=True), name="static")

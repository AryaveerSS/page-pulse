"""
App entrypoint: wires up the router, CORS, static frontend, and the
exception -> HTTP response mapping.

Design decision: every AuditError subclass is caught by a single handler
here and turned into the same ErrorResponse shape with its own status_code.
Adding a new failure mode later means adding one exception class in
core/exceptions.py - nothing here has to change.
"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.exceptions import AuditError
from app.routers import audit

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Audits any URL and reports on-page health metrics.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(audit.router)


@app.exception_handler(AuditError)
async def audit_error_handler(request: Request, exc: AuditError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error_code": exc.error_code, "message": exc.message},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
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

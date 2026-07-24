# Page Pulse

A FastAPI URL auditing tool built as a portfolio project for Digital Heroes. Audits any URL and returns on-page health metrics: HTTP status, response time, title, meta description, H1 count, image alt-text coverage, and word count.

**Live demo:** _add your Render URL here_
**API docs (Swagger):** `<your-live-url>/docs`

---

## Tech Stack

| Layer | Technology |
|---|---|
| API framework | FastAPI |
| Runtime | Python 3.11+ |
| HTTP client | httpx (async) |
| HTML parsing | BeautifulSoup4 + lxml |
| Schema validation | Pydantic v2 |
| Rate limiting | slowapi |
| Server | uvicorn |

---

## Setup

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000` for the UI, or `http://127.0.0.1:8000/docs` for the interactive Swagger reference.

### Running Tests

```bash
pytest -v
```

31 tests across 4 files:

| File | Tests | What it covers |
|---|---|---|
| `test_parser.py` | 8 | Pure unit tests over parsing logic — no mocking, deterministic, instant |
| `test_api.py` | 7 | Integration tests with `respx` mocking outbound HTTP |
| `test_retry.py` | 8 | Verifies transient errors retry, permanent errors don't |
| `test_batch.py` | 8 | Partial failure isolation, result ordering, batch validation |

---

## API Reference

### `POST /api/audit`

Audits a single URL.

**Request**
```json
{ "url": "https://example.com" }
```

**Response — 200**
```json
{
  "url": "https://example.com/",
  "requested_url": "https://example.com",
  "http_status": 200,
  "response_time_ms": 138,
  "attempts": 1,
  "title": "Example Domain",
  "meta_description": "An example page for testing.",
  "h1_count": 1,
  "image_count": 3,
  "images_missing_alt": 2,
  "word_count": 11,
  "audited_at": "2026-07-25T00:00:00Z"
}
```

The `attempts` field reflects retry behaviour — a transient failure followed by a successful retry returns `attempts: 2`.

**Error responses** always use the same envelope:
```json
{ "error_code": "fetch_timeout", "message": "Target did not respond within 8.0s." }
```

| Status | `error_code` | Meaning | Retried? |
|---|---|---|---|
| 422 | `validation_error` | Malformed or non-http(s) URL | — |
| 413 | `content_too_large` | Target page > 5MB | No |
| 415 | `unsupported_content_type` | Response wasn't HTML | No |
| 429 | `rate_limit_exceeded` | Too many requests | — |
| 502 | `unreachable_url` | DNS/connection failure | Yes |
| 502 | `too_many_redirects` | Redirect loop | No |
| 502 | `upstream_http_error` | Target returned 4xx | No |
| 502 | `upstream_http_error` | Target returned non-transient 5xx | No |
| 504 | `fetch_timeout` | Request timed out | Yes |

---

### `POST /api/audit/batch`

Audits 1–10 URLs concurrently. Always returns HTTP 200 — per-URL errors are reported in the response body, not via HTTP status.

**Request**
```json
{ "urls": ["https://example.com", "https://example.org"] }
```

**Response — 200**
```json
{
  "total": 2,
  "succeeded": 1,
  "failed": 1,
  "results": [
    { "url": "https://example.com", "success": true, "report": { "..." : "..." } },
    { "url": "https://example.org", "success": false, "error_code": "fetch_timeout", "error_message": "..." }
  ],
  "audited_at": "2026-07-25T00:00:00Z"
}
```

---

### `GET /api/health`

Liveness check. Exempt from rate limiting.

```json
{ "status": "ok" }
```

---

## Configuration

All settings use the `PAGEPULSE_` prefix and can be overridden via environment variables.

| Variable | Default | Description |
|---|---|---|
| `PAGEPULSE_FETCH_TIMEOUT_SECONDS` | `8.0` | Per-request timeout |
| `PAGEPULSE_MAX_RETRY_ATTEMPTS` | `2` | Total attempts (1 retry) |
| `PAGEPULSE_RETRY_BACKOFF_SECONDS` | `0.5` | Sleep between retries |
| `PAGEPULSE_BATCH_MAX_URLS` | `10` | Maximum URLs per batch request |
| `PAGEPULSE_RATE_LIMIT` | `"30/minute"` | Rate limit applied per IP |
| `PAGEPULSE_MAX_CONTENT_BYTES` | `5242880` | Response body cap (5MB) |

---

## Engineering Decisions

### 1. Retry logic with transient vs permanent failure classification

Not all errors are equal, and the retry logic is explicit about that.

Every `AuditError` subclass carries a `retryable: bool` flag. The fetch loop reads that flag — it never inspects HTTP status codes directly to decide whether to retry.

- **Retryable (transient):** `FetchTimeoutError`, `UnreachableURLError`, `TransientUpstreamError` (429/500/502/503/504)
- **Not retryable (permanent):** `UpstreamHTTPError` (4xx), `UnsupportedContentTypeError`, `ContentTooLargeError`, `TooManyRedirectsError`

Retrying a 404 is pointless and wastes time. Retrying a timeout or a 503 is cheap and often succeeds. `fetch_page()` retries up to `max_retry_attempts` (default: 2 total, 1 retry) with an `asyncio.sleep` backoff between attempts. The `AuditReport` response includes an `attempts` field so callers can see whether a retry occurred.

Adding a new failure mode means adding one new exception class. The retry loop and the HTTP handler in `main.py` pick it up automatically.

---

### 2. Concurrent batch auditing with `asyncio.gather(return_exceptions=True)`

`POST /api/audit/batch` accepts 1–10 URLs and runs all audits in parallel. The key design choice is `return_exceptions=True` — exceptions from individual tasks are captured as values rather than propagating, so one failing URL never aborts the others.

Each result carries its own `success` flag with `report` on success and `error_code`/`error_message` on failure. The batch endpoint always returns HTTP 200; the aggregate `succeeded`/`failed` counts at the top level give a quick summary without requiring clients to iterate the full results array.

---

### 3. Per-IP rate limiting via slowapi

All audit endpoints are limited to 30 requests/minute per IP, configurable via `PAGEPULSE_RATE_LIMIT`. `/api/health` is explicitly exempt.

Rate limit responses use the same `{ error_code, message }` envelope as all other errors — clients don't need to handle a different shape for 429s. The limit is intentionally permissive enough for normal interactive use while blocking runaway scripts on the public demo.

---

## Architecture

```
app/
├── main.py              # App factory, global exception handlers, router registration
├── core/
│   ├── config.py        # Pydantic Settings — all PAGEPULSE_ env vars in one place
│   ├── exceptions.py    # Typed AuditError hierarchy with status_code, error_code, retryable
│   └── limiter.py       # slowapi Limiter singleton
├── routers/
│   └── audit.py         # Route handlers — thin, delegates to audit_service
├── schemas/
│   └── audit.py         # Pydantic request/response models (single + batch)
└── services/
    ├── fetcher.py        # Only file that touches the network — returns FetchResult or raises
    ├── parser.py         # Pure functions over HTML strings, no network knowledge
    └── audit_service.py  # Orchestrates fetch → parse → AuditReport
```

**Fetch/parse separation** — `fetcher.py` is the only network-touching module. `parser.py` is pure functions over an HTML string. This makes `test_parser.py`'s 8 tests deterministic and instant — no mocking. The networking layer can be completely rewritten without touching the parsing tests.

**Typed exception hierarchy** — every anticipated failure mode is its own `AuditError` subclass in `core/exceptions.py`, each carrying `status_code`, `error_code`, and `retryable`. A single handler in `main.py` turns any of them into the same JSON envelope. Every future endpoint gets consistent error handling for free.

**Word count excludes `<head>` content** — `soup.get_text()` counts `<title>` and `<meta>` text, inflating the count and measuring the wrong thing. The implementation decomposes `<head>` before counting, measuring what a visitor actually reads.

---

## What I'd Improve with More Time

- **SQLite cache with short TTL** — the same URL re-fetches on every request. A 60s TTL cache would cut redundant outbound fetches significantly.
- **Meaningful alt-text detection** — the current check is binary: present or missing. A better version would flag `alt="image123.jpg"`-style values as low-quality.
- **Streaming batch responses via SSE** — results appear as each URL completes rather than waiting for all of them.
- **Webhook mode** — accept a `callback_url` in the batch request and POST results there, enabling fire-and-forget audits.

---

## Deployment (Render, Free Tier)

1. Push this repo to GitHub.
2. On [render.com](https://render.com): New → Web Service → connect the repo.
   `render.yaml` auto-configures build and start commands, or set manually:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. Deploy.

> **Note:** Render's free tier spins down after 15 minutes of inactivity. The first request after an idle period takes 30–60s. Worth mentioning to a reviewer who hits a cold start.

---

## Where I Used AI, and What I Changed

_Replace this paragraph before submitting. Describe specifically where you used AI assistance — scaffolding the FastAPI structure, generating initial test cases, designing the exception hierarchy, etc. — and what you reviewed, changed, or disagreed with afterward. Reviewers are looking for evidence that the decisions in this README reflect your own judgment. The strongest version of this paragraph names one concrete thing you pushed back on or changed._

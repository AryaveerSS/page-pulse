# Page Pulse

A URL health auditor built with FastAPI. Point it at any URL and it fetches the page, then returns a structured report: HTTP status, response time, title, meta description, heading structure, image alt-text coverage, and word count.

**Live demo:** https://page-pulse-uc36.onrender.com
**Interactive API docs:** https://page-pulse-uc36.onrender.com/docs

> First load after a period of inactivity takes 30–60 seconds — Render's free tier spins down idle services. Subsequent requests are fast.

---

## Setup

Requires Python 3.11+.

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000` for the UI, or `http://127.0.0.1:8000/docs` for the Swagger reference.

### Running the tests

```bash
pytest -v
```

109 tests, all passing. No network calls — outbound HTTP is intercepted by `respx`.

---

## API contract

### `POST /api/audit` — audit a single URL

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
  "response_time_ms": 142,
  "attempts": 1,
  "title": "Example Domain",
  "meta_description": "An example page.",
  "h1_count": 1,
  "image_count": 0,
  "images_missing_alt": 0,
  "word_count": 28,
  "audited_at": "2026-07-25T10:00:00Z"
}
```

The `attempts` field is `1` on a clean first-try success. If a transient failure (timeout, 503) was retried and then succeeded, it will be `2`.

**Error responses** — every failure mode returns the same shape:
```json
{ "error_code": "fetch_timeout", "message": "Target did not respond within 8.0s." }
```

| Status | `error_code` | Cause | Retried automatically? |
|--------|-------------|-------|------------------------|
| 422 | `validation_error` | Malformed or non-http(s) URL | — |
| 413 | `content_too_large` | Page exceeded the 5 MB cap | No |
| 415 | `unsupported_content_type` | Response was not HTML | No |
| 429 | `rate_limit_exceeded` | More than 30 requests/minute from one IP | — |
| 502 | `unreachable_url` | DNS failure or connection refused | Yes |
| 502 | `too_many_redirects` | Redirect loop | No |
| 502 | `upstream_http_error` | Target returned a 4xx | No |
| 504 | `fetch_timeout` | Target did not respond in time | Yes |

---

### `POST /api/audit/batch` — audit up to 10 URLs in parallel

**Request**
```json
{ "urls": ["https://example.com", "https://example.org"] }
```

**Response — 200**

The batch endpoint always returns HTTP 200. One failing URL never causes the others to be skipped — each result carries its own `success` flag.

```json
{
  "total": 2,
  "succeeded": 1,
  "failed": 1,
  "results": [
    {
      "url": "https://example.com",
      "success": true,
      "report": { "...": "full AuditReport fields" }
    },
    {
      "url": "https://example.org",
      "success": false,
      "error_code": "fetch_timeout",
      "error_message": "Target did not respond within 8.0s."
    }
  ],
  "audited_at": "2026-07-25T10:00:00Z"
}
```

---

### `GET /api/health`

Liveness check. Returns `{ "status": "ok" }`. Exempt from rate limiting.

---

## Design decisions

### 1. Fetching and parsing are separate modules

`services/fetcher.py` is the only file that touches the network. It returns a plain `FetchResult` object (URL, status code, timing, raw HTML) or raises a typed exception. `services/parser.py` is pure functions over an HTML string — it has no idea a network exists.

**Why this matters:** The parser tests (`test_parser.py`) are 43 pure function calls with hand-written HTML fixtures. No mocking, no server, no async — just input in, dict out. They run in under a second and can never have a false positive from a network condition. If parsing logic changes, only parser tests break. If the HTTP client library changes, only fetcher tests break. The two failure modes stay isolated.

The alternative — one `audit(url)` function that fetches and parses — would work for a prototype but makes testing harder as the codebase grows: every parser test would need to either hit the network or mock httpx.

---

### 2. Every error is a typed exception class with a `retryable` flag

Every failure mode the tool can encounter — timeout, DNS failure, 404, wrong content type, page too large — is its own subclass of `AuditError` in `core/exceptions.py`. Each class carries three things: the HTTP status code to return, a machine-readable `error_code` string, and a `retryable: bool` flag.

```
FetchTimeoutError      → status 504, retryable=True   (server might respond next time)
UnreachableURLError    → status 502, retryable=True   (could be a momentary network blip)
TransientUpstreamError → status 502, retryable=True   (503 overload — worth retrying)
UpstreamHTTPError      → status 502, retryable=False  (404/403 won't change on retry)
UnsupportedContentType → status 415, retryable=False  (content-type won't change)
```

The retry loop in `fetch_page()` reads `exc.retryable` directly — it never inspects HTTP status codes with `if status == 503`. This means the retry policy is encoded in the type definition, not scattered across conditional logic. Adding a new failure mode means adding one class; the retry loop and the FastAPI error handler both pick it up automatically.

A single exception handler in `main.py` converts any `AuditError` subclass into the `{ error_code, message }` JSON envelope. Consumers only ever see one error shape regardless of what went wrong.

---

### 3. Word count excludes `<head>` content

The naive implementation of word count — `soup.get_text()` — counts text from the entire document, including the `<title>` tag and `<meta>` descriptions. That inflates the number and measures the wrong thing: a page with a long, keyword-stuffed title would show a higher word count even if the body has almost nothing in it.

The implementation decomposes `<head>` before counting, and also strips `<script>`, `<style>`, and `<noscript>` blocks. The result is an approximation of what a visitor actually reads on the page, which is the more useful signal for a page health audit.

This is a small call, but it's the kind of judgment that matters: "approximate word count" is ambiguous until you decide whose count it is — the HTML document's, or the reader's. This implementation chooses the reader's.

---

## Where I used AI, and what I changed

I used AI (Claude) throughout — to scaffold the FastAPI project structure, generate the initial test suite, and speed up the UI styling. The AI generated the UI skeleton, but the cream/green colour scheme and Digital Heroes branding decisions were directed by me to match the client's existing website. The two things I specifically changed on the backend: the AI's first retry implementation checked HTTP status codes directly in the loop, which I replaced with a `retryable` flag on each exception class so the policy lives in the type definition rather than scattered conditionals. I also split the original single `UpstreamHTTPError` into two classes — permanent 4xx errors that should never be retried, and transient 5xx errors that should — after realising the original design would have retried a 404, which is pointless.

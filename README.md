# Page Pulse

A small tool that audits any URL: fetches the page and reports on-page health
metrics — HTTP status, response time, title, meta description, heading
structure, image alt-text coverage, and approximate word count.

**Live demo:** _add your Render URL here after deploying_
**API docs (Swagger):** `<your-live-url>/docs`

---

## Setup

Requires Python 3.11+.

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt

uvicorn app.main:app --reload   # serves the app at http://127.0.0.1:8000
```

Open `http://127.0.0.1:8000` for the UI, or `http://127.0.0.1:8000/docs` for
the interactive API reference.

### Running tests

```bash
pytest -v
```

15 tests: 8 pure unit tests over the parsing logic (`tests/test_parser.py`)
and 7 tests over the live API with the network mocked (`tests/test_api.py`),
covering the happy path plus timeout, connection-failure, non-HTML,
upstream-4xx, and malformed-URL cases.

---

## API contract

### `POST /api/audit`

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
  "title": "Example Domain",
  "meta_description": "An example page for testing.",
  "h1_count": 1,
  "image_count": 3,
  "images_missing_alt": 2,
  "word_count": 11,
  "audited_at": "2026-07-24T19:11:01.499598Z"
}
```

**Error responses** — every failure mode returns the same shape:
```json
{ "error_code": "fetch_timeout", "message": "Target did not respond within 8.0s." }
```

| Status | `error_code`                  | Meaning                                      |
|--------|-------------------------------|-----------------------------------------------|
| 422    | `validation_error`            | Malformed or non-http(s) URL                  |
| 413    | `content_too_large`           | Target page exceeded the 5MB size cap         |
| 415    | `unsupported_content_type`    | Target response wasn't HTML                   |
| 502    | `unreachable_url`             | DNS/connection failure                        |
| 502    | `too_many_redirects`          | Exceeded the redirect limit                   |
| 502    | `upstream_http_error`         | Target responded with a 4xx/5xx                |
| 504    | `fetch_timeout`               | Target didn't respond in time                 |

### `GET /api/health`
Liveness check, returns `{"status": "ok"}`.

---

## Design decisions

**1. Fetching and parsing are separate modules, not one function.**
`services/fetcher.py` is the only file that touches the network — it
returns a plain `FetchResult` (url, status, timing, raw HTML) or raises a
typed exception. `services/parser.py` is pure functions over an HTML
string, with no knowledge that a network exists. This is what makes
`test_parser.py`'s 8 tests deterministic and instant (no mocking) while
`test_api.py` separately verifies the fetch-to-HTTP-status wiring with
`respx`. If a parsing bug shows up, its test survives untouched even if the
networking layer is completely rewritten later.

**2. Errors are a typed exception hierarchy, not scattered `try/except` blocks.**
Every anticipated failure — timeout, DNS failure, non-HTML content, oversized
page, upstream 4xx/5xx — is its own `AuditError` subclass in
`core/exceptions.py`, each carrying its own HTTP status code. A single
FastAPI exception handler in `main.py` turns any of them into the same
`{error_code, message}` JSON shape. The alternative (checking error types
inline in the router with `try/except httpx.TimeoutException: raise
HTTPException(504, ...)`) works for one endpoint, but doesn't scale — this
version means every future endpoint gets consistent error handling for free,
and the full list of failure modes is visible in one file instead of buried
across the codebase.

**3. Word count deliberately excludes `<head>` content.**
Early on, the naive implementation (`soup.get_text()`) counted `<title>` and
`<meta>` text toward the word count, which inflates the number for pages
with long titles and doesn't reflect what a visitor actually reads. A test
caught this (`test_word_count_excludes_head_content`), and the fix decomposes
`<head>` before counting. This is a small thing, but it's the kind of
judgment call the brief calls out — "approximate word count" is ambiguous
until you decide whose count it is: the browser tab's, or the reader's. I
chose the reader's.

**Other calls worth naming:** an empty `alt=""` is counted as "missing" even
though it's technically valid markup for decorative images — there's no way
to distinguish "decorative, intentionally empty" from "forgotten" from
markup alone, so the audit errs toward flagging it. Response bodies are
streamed and capped at 5MB rather than fully buffered, so a URL that returns
an enormous or infinite stream can't hang or crash the server.

---

## What I'd change with another day

The alt-text check only looks at the `alt` attribute; it doesn't check
whether `alt` text is *meaningful* (e.g. `alt="image123.jpg"` currently
passes). A stronger version would flag suspiciously filename-like or
single-word alt text as a separate "low-quality alt text" signal rather
than a binary present/absent check. I'd also add a small SQLite cache
keyed by URL + a short TTL, since the tool as written re-fetches on every
request even for the same URL scanned seconds apart.

---

## Where I used AI, and what I changed

_Personalize this paragraph before submitting — describe, in your own words,
where you used AI (e.g. scaffolding the FastAPI structure, generating the
initial test cases) and what you specifically reviewed, changed, or rejected
afterward. The reviewers are explicitly checking that this reflects your own
judgment, not a first-draft dump — the strongest version of this paragraph
names one thing you disagreed with or changed._

---

## Deployment (Render, free tier)

1. Push this repo to GitHub (public).
2. On [render.com](https://render.com), New → Web Service → connect the repo.
   `render.yaml` in this repo will auto-configure the build/start commands,
   or set manually:
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. Deploy. First load after idle periods takes ~30-60s (free tier spins
   down after 15 min of inactivity) — mention this if a reviewer hits a
   cold start.

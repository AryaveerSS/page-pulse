"""
Tests for the POST /api/audit/batch endpoint.

Key invariants:
  - All URLs are audited concurrently (asyncio.gather).
  - One failing URL never prevents the others from being returned.
  - Success/failure state is per-URL, not per-request.
  - The response counts (total, succeeded, failed) are always consistent.
  - Oversized batches are rejected with 422 before any network activity.
"""

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.main import app

VALID_HTML = "<html><head><title>Test</title></head><body><h1>Hi</h1></body></html>"


# ---------------------------------------------------------------------------
# Client fixture with rate-limiter reset
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """
    Clear the in-memory rate-limit counters between tests so tests
    never interfere with each other's quota.
    """
    from app.core.limiter import limiter

    limiter._storage.reset()
    yield
    limiter._storage.reset()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_batch_all_succeed(client):
    """All valid URLs return success entries; counts are consistent."""
    for url in ("https://site-a.test/", "https://site-b.test/"):
        respx.get(url).mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )

    resp = await client.post(
        "/api/audit/batch",
        json={"urls": ["https://site-a.test/", "https://site-b.test/"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["succeeded"] == 2
    assert body["failed"] == 0
    assert all(item["success"] for item in body["results"])
    # Each successful item must carry a report with expected fields.
    for item in body["results"]:
        assert item["report"]["title"] == "Test"
        assert item["report"]["h1_count"] == 1


# ---------------------------------------------------------------------------
# Partial failure — one bad URL doesn't kill the rest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_batch_partial_failure_does_not_abort_others(client):
    """
    If one URL fails (404), the other URLs still complete successfully.
    This is the core contract of the batch endpoint.
    """
    respx.get("https://good.test/").mock(
        return_value=httpx.Response(
            200, text=VALID_HTML, headers={"content-type": "text/html"}
        )
    )
    respx.get("https://missing.test/").mock(
        return_value=httpx.Response(404, text="not found")
    )

    resp = await client.post(
        "/api/audit/batch",
        json={"urls": ["https://good.test/", "https://missing.test/"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["succeeded"] == 1
    assert body["failed"] == 1

    results_by_url = {item["url"]: item for item in body["results"]}
    assert results_by_url["https://good.test/"]["success"] is True
    assert results_by_url["https://missing.test/"]["success"] is False
    assert (
        results_by_url["https://missing.test/"]["error_code"] == "upstream_http_error"
    )


@pytest.mark.asyncio
@respx.mock
async def test_batch_all_fail_returns_200_with_failure_items(client):
    """
    Even if every URL fails, the batch endpoint returns 200 — the per-URL
    failure state is inside the response body, not the HTTP status code.
    """
    respx.get("https://gone-a.test/").mock(
        return_value=httpx.Response(404, text="not found")
    )
    respx.get("https://gone-b.test/").mock(
        return_value=httpx.Response(410, text="gone")
    )

    resp = await client.post(
        "/api/audit/batch",
        json={"urls": ["https://gone-a.test/", "https://gone-b.test/"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["succeeded"] == 0
    assert body["failed"] == 2
    for item in body["results"]:
        assert item["success"] is False
        assert item["error_code"] is not None


@pytest.mark.asyncio
@respx.mock
async def test_batch_connection_error_is_per_url(client):
    """A network error on one URL is isolated to that URL's result."""
    respx.get("https://ok.test/").mock(
        return_value=httpx.Response(
            200, text=VALID_HTML, headers={"content-type": "text/html"}
        )
    )
    respx.get("https://down.test/").mock(side_effect=httpx.ConnectError("refused"))

    resp = await client.post(
        "/api/audit/batch",
        json={"urls": ["https://ok.test/", "https://down.test/"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["succeeded"] == 1
    assert body["failed"] == 1

    results_by_url = {item["url"]: item for item in body["results"]}
    assert results_by_url["https://ok.test/"]["success"] is True
    assert results_by_url["https://down.test/"]["error_code"] == "unreachable_url"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_rejects_empty_url_list(client):
    """An empty list should be rejected with 422."""
    resp = await client.post("/api/audit/batch", json={"urls": []})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_batch_rejects_oversized_request(client):
    """Requests with more than batch_max_urls URLs should be rejected with 422."""
    from app.core.config import settings

    urls = [f"https://site-{i}.test/" for i in range(settings.batch_max_urls + 1)]
    resp = await client.post("/api/audit/batch", json={"urls": urls})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_batch_rejects_invalid_url_in_list(client):
    """A malformed URL anywhere in the list should trigger a 422."""
    resp = await client.post(
        "/api/audit/batch",
        json={"urls": ["https://good.test/", "not-a-url"]},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Response ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_batch_results_preserve_submission_order(client):
    """Results must be returned in the same order as the submitted URLs."""
    urls = [f"https://ordered-{i}.test/" for i in range(3)]
    for url in urls:
        respx.get(url).mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )

    resp = await client.post("/api/audit/batch", json={"urls": urls})

    assert resp.status_code == 200
    returned_urls = [item["url"] for item in resp.json()["results"]]
    assert returned_urls == urls

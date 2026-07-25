"""
Tests for POST /api/audit/batch.

Core contract being tested:
  - All URLs run concurrently; one failure never aborts others.
  - Success/failure is per-URL, never per-request (HTTP always 200).
  - Response counts (total, succeeded, failed) are always internally consistent.
  - Results are returned in submission order regardless of completion order.
  - Input validation rejects bad payloads before any network activity.
  - The response schema is complete and correct on both success and failure paths.
"""

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.main import app

VALID_HTML = "<html><head><title>Test</title></head><body><h1>Hi</h1></body></html>"
RICH_HTML = """<html>
  <head>
    <title>Rich Page</title>
    <meta name="description" content="A rich page.">
  </head>
  <body>
    <h1>Main heading</h1>
    <img src="a.png" alt="icon">
    <p>Seven visible words on this page.</p>
  </body>
</html>"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_rate_limiter():
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
# Happy path — all succeed
# ---------------------------------------------------------------------------


class TestAllSucceed:
    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_200(self, client):
        for url in ("https://a.test/", "https://b.test/"):
            respx.get(url).mock(
                return_value=httpx.Response(
                    200, text=VALID_HTML, headers={"content-type": "text/html"}
                )
            )
        resp = await client.post(
            "/api/audit/batch", json={"urls": ["https://a.test/", "https://b.test/"]}
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    @respx.mock
    async def test_counts_are_consistent(self, client):
        for url in ("https://a.test/", "https://b.test/"):
            respx.get(url).mock(
                return_value=httpx.Response(
                    200, text=VALID_HTML, headers={"content-type": "text/html"}
                )
            )
        body = (
            await client.post(
                "/api/audit/batch",
                json={"urls": ["https://a.test/", "https://b.test/"]},
            )
        ).json()
        assert body["total"] == 2
        assert body["succeeded"] == 2
        assert body["failed"] == 0
        assert body["total"] == body["succeeded"] + body["failed"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_each_result_has_success_true(self, client):
        for url in ("https://a.test/", "https://b.test/"):
            respx.get(url).mock(
                return_value=httpx.Response(
                    200, text=VALID_HTML, headers={"content-type": "text/html"}
                )
            )
        body = (
            await client.post(
                "/api/audit/batch",
                json={"urls": ["https://a.test/", "https://b.test/"]},
            )
        ).json()
        assert all(item["success"] for item in body["results"])

    @pytest.mark.asyncio
    @respx.mock
    async def test_success_items_contain_full_report(self, client):
        respx.get("https://rich.test/").mock(
            return_value=httpx.Response(
                200, text=RICH_HTML, headers={"content-type": "text/html"}
            )
        )
        body = (
            await client.post("/api/audit/batch", json={"urls": ["https://rich.test/"]})
        ).json()
        report = body["results"][0]["report"]

        assert report["title"] == "Rich Page"
        assert report["meta_description"] == "A rich page."
        assert report["h1_count"] == 1
        assert report["image_count"] == 1
        assert report["images_missing_alt"] == 0
        assert report["http_status"] == 200
        assert "audited_at" in report

    @pytest.mark.asyncio
    @respx.mock
    async def test_single_url_batch_works(self, client):
        """A batch of 1 is valid and should behave identically to /api/audit."""
        respx.get("https://single.test/").mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )
        body = (
            await client.post(
                "/api/audit/batch", json={"urls": ["https://single.test/"]}
            )
        ).json()
        assert body["total"] == 1
        assert body["succeeded"] == 1
        assert body["results"][0]["success"] is True

    @pytest.mark.asyncio
    @respx.mock
    async def test_response_has_audited_at_timestamp(self, client):
        respx.get("https://a.test/").mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )
        body = (
            await client.post("/api/audit/batch", json={"urls": ["https://a.test/"]})
        ).json()
        assert "audited_at" in body
        from datetime import datetime

        datetime.fromisoformat(body["audited_at"].replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Partial failure — one bad URL must not abort others
# ---------------------------------------------------------------------------


class TestPartialFailure:
    @pytest.mark.asyncio
    @respx.mock
    async def test_one_404_does_not_abort_other_urls(self, client):
        respx.get("https://good.test/").mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )
        respx.get("https://missing.test/").mock(
            return_value=httpx.Response(404, text="not found")
        )

        body = (
            await client.post(
                "/api/audit/batch",
                json={"urls": ["https://good.test/", "https://missing.test/"]},
            )
        ).json()

        assert body["succeeded"] == 1
        assert body["failed"] == 1
        by_url = {r["url"]: r for r in body["results"]}
        assert by_url["https://good.test/"]["success"] is True
        assert by_url["https://missing.test/"]["success"] is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_failure_item_has_error_fields(self, client):
        respx.get("https://missing.test/").mock(
            return_value=httpx.Response(404, text="not found")
        )
        body = (
            await client.post(
                "/api/audit/batch", json={"urls": ["https://missing.test/"]}
            )
        ).json()
        item = body["results"][0]

        assert item["success"] is False
        assert item["error_code"] == "upstream_http_error"
        assert isinstance(item["error_message"], str)
        assert len(item["error_message"]) > 0
        assert item["report"] is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_all_fail_still_returns_200(self, client):
        """HTTP 200 always — errors are communicated inside the body."""
        respx.get("https://gone-a.test/").mock(
            return_value=httpx.Response(404, text="nope")
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
        assert body["succeeded"] == 0
        assert body["failed"] == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_connection_error_isolated_to_url(self, client):
        respx.get("https://ok.test/").mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )
        respx.get("https://down.test/").mock(side_effect=httpx.ConnectError("refused"))
        body = (
            await client.post(
                "/api/audit/batch",
                json={"urls": ["https://ok.test/", "https://down.test/"]},
            )
        ).json()

        by_url = {r["url"]: r for r in body["results"]}
        assert by_url["https://ok.test/"]["success"] is True
        assert by_url["https://down.test/"]["error_code"] == "unreachable_url"

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_error_isolated_to_url(self, client):
        respx.get("https://ok.test/").mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )
        respx.get("https://slow.test/").mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        body = (
            await client.post(
                "/api/audit/batch",
                json={"urls": ["https://ok.test/", "https://slow.test/"]},
            )
        ).json()

        by_url = {r["url"]: r for r in body["results"]}
        assert by_url["https://slow.test/"]["error_code"] == "fetch_timeout"

    @pytest.mark.asyncio
    @respx.mock
    async def test_mixed_error_types_in_same_batch(self, client):
        """Multiple different error types in one batch — each reported correctly."""
        respx.get("https://ok.test/").mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )
        respx.get("https://gone.test/").mock(
            return_value=httpx.Response(404, text="nope")
        )
        respx.get("https://slow.test/").mock(side_effect=httpx.TimeoutException("slow"))

        body = (
            await client.post(
                "/api/audit/batch",
                json={
                    "urls": [
                        "https://ok.test/",
                        "https://gone.test/",
                        "https://slow.test/",
                    ]
                },
            )
        ).json()

        assert body["total"] == 3
        assert body["succeeded"] == 1
        assert body["failed"] == 2
        by_url = {r["url"]: r for r in body["results"]}
        assert by_url["https://ok.test/"]["success"] is True
        assert by_url["https://gone.test/"]["error_code"] == "upstream_http_error"
        assert by_url["https://slow.test/"]["error_code"] == "fetch_timeout"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_rejects_empty_list(self, client):
        resp = await client.post("/api/audit/batch", json={"urls": []})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_rejects_oversized_batch(self, client):
        from app.core.config import settings

        urls = [f"https://site-{i}.test/" for i in range(settings.batch_max_urls + 1)]
        resp = await client.post("/api/audit/batch", json={"urls": urls})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_rejects_malformed_url_in_list(self, client):
        resp = await client.post(
            "/api/audit/batch", json={"urls": ["https://good.test/", "not-a-url"]}
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_rejects_missing_urls_field(self, client):
        resp = await client.post("/api/audit/batch", json={})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_validation_error_uses_standard_envelope(self, client):
        body = (await client.post("/api/audit/batch", json={"urls": []})).json()
        assert "error_code" in body
        assert "message" in body


# ---------------------------------------------------------------------------
# Result ordering
# ---------------------------------------------------------------------------


class TestResultOrdering:
    @pytest.mark.asyncio
    @respx.mock
    async def test_results_in_submission_order(self, client):
        """asyncio.gather preserves input order regardless of completion order."""
        urls = [f"https://ordered-{i}.test/" for i in range(5)]
        for url in urls:
            respx.get(url).mock(
                return_value=httpx.Response(
                    200, text=VALID_HTML, headers={"content-type": "text/html"}
                )
            )
        body = (await client.post("/api/audit/batch", json={"urls": urls})).json()
        assert [r["url"] for r in body["results"]] == urls

    @pytest.mark.asyncio
    @respx.mock
    async def test_mixed_success_failure_order_preserved(self, client):
        """Order must be preserved even when results have different types."""
        respx.get("https://first.test/").mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )
        respx.get("https://second.test/").mock(
            return_value=httpx.Response(404, text="nope")
        )
        respx.get("https://third.test/").mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )

        body = (
            await client.post(
                "/api/audit/batch",
                json={
                    "urls": [
                        "https://first.test/",
                        "https://second.test/",
                        "https://third.test/",
                    ]
                },
            )
        ).json()

        results = body["results"]
        assert results[0]["url"] == "https://first.test/"
        assert results[0]["success"] is True
        assert results[1]["url"] == "https://second.test/"
        assert results[1]["success"] is False
        assert results[2]["url"] == "https://third.test/"
        assert results[2]["success"] is True

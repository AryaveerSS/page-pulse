"""
Integration tests for the /api/audit endpoint.

These tests verify the full request → fetch → parse → response pipeline
using respx to intercept outbound HTTP calls.  They are the only tests
that exercise the router, exception handlers, and response schema together.

What these tests prove:
  - Every anticipated failure mode maps to the right HTTP status and error_code
  - The happy-path response shape is complete and correct (including new fields)
  - The error envelope is always {error_code, message} regardless of failure type
  - Input validation rejects malformed URLs before any network call is made
  - The /api/health liveness probe is exempt from all error handling
"""

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.main import app

VALID_HTML = "<html><head><title>Test Page</title><meta name='description' content='A test.'></head><body><h1>Hi</h1><img src='a.png' alt='icon'></body></html>"
MINIMAL_HTML = "<html><head><title>Min</title></head><body><h1>Hello</h1></body></html>"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Prevent rate-limit state leaking between tests."""
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
# Happy path — response shape
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_200_on_valid_html_page(self, client):
        respx.get("https://good-site.test/").mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )
        resp = await client.post("/api/audit", json={"url": "https://good-site.test/"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    @respx.mock
    async def test_response_contains_all_required_fields(self, client):
        respx.get("https://good-site.test/").mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )
        body = (
            await client.post("/api/audit", json={"url": "https://good-site.test/"})
        ).json()

        required = {
            "url",
            "requested_url",
            "http_status",
            "response_time_ms",
            "attempts",
            "title",
            "meta_description",
            "h1_count",
            "image_count",
            "images_missing_alt",
            "word_count",
            "audited_at",
        }
        assert required.issubset(body.keys())

    @pytest.mark.asyncio
    @respx.mock
    async def test_parsed_fields_are_accurate(self, client):
        respx.get("https://good-site.test/").mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )
        body = (
            await client.post("/api/audit", json={"url": "https://good-site.test/"})
        ).json()

        assert body["title"] == "Test Page"
        assert body["meta_description"] == "A test."
        assert body["h1_count"] == 1
        assert body["http_status"] == 200
        assert body["image_count"] == 1
        assert body["images_missing_alt"] == 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_response_time_is_non_negative_integer(self, client):
        respx.get("https://good-site.test/").mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )
        body = (
            await client.post("/api/audit", json={"url": "https://good-site.test/"})
        ).json()
        assert isinstance(body["response_time_ms"], int)
        assert body["response_time_ms"] >= 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_attempts_is_1_on_clean_success(self, client):
        respx.get("https://good-site.test/").mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )
        body = (
            await client.post("/api/audit", json={"url": "https://good-site.test/"})
        ).json()
        assert body["attempts"] == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_requested_url_preserved_before_redirects(self, client):
        """requested_url must reflect what was submitted, not the final URL."""
        respx.get("https://good-site.test/").mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )
        body = (
            await client.post("/api/audit", json={"url": "https://good-site.test/"})
        ).json()
        assert body["requested_url"] == "https://good-site.test/"

    @pytest.mark.asyncio
    @respx.mock
    async def test_audited_at_is_iso8601_timestamp(self, client):
        respx.get("https://good-site.test/").mock(
            return_value=httpx.Response(
                200, text=VALID_HTML, headers={"content-type": "text/html"}
            )
        )
        body = (
            await client.post("/api/audit", json={"url": "https://good-site.test/"})
        ).json()
        from datetime import datetime

        # Must parse without raising
        dt = datetime.fromisoformat(body["audited_at"].replace("Z", "+00:00"))
        assert dt.tzinfo is not None  # must be timezone-aware


# ---------------------------------------------------------------------------
# Input validation — 422
# ---------------------------------------------------------------------------


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_rejects_non_url_string(self, client):
        resp = await client.post("/api/audit", json={"url": "not-a-url"})
        assert resp.status_code == 422
        assert resp.json()["error_code"] == "validation_error"

    @pytest.mark.asyncio
    async def test_rejects_missing_url_field(self, client):
        resp = await client.post("/api/audit", json={})
        assert resp.status_code == 422
        assert resp.json()["error_code"] == "validation_error"

    @pytest.mark.asyncio
    async def test_rejects_non_http_scheme(self, client):
        resp = await client.post("/api/audit", json={"url": "ftp://example.com"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_error_envelope_shape_on_validation_failure(self, client):
        body = (await client.post("/api/audit", json={"url": "bad"})).json()
        assert "error_code" in body
        assert "message" in body
        assert isinstance(body["message"], str)


# ---------------------------------------------------------------------------
# Network / upstream failure modes
# ---------------------------------------------------------------------------


class TestFailureModes:
    @pytest.mark.asyncio
    @respx.mock
    async def test_connection_error_returns_502_unreachable(self, client):
        respx.get("https://unreachable.test/").mock(
            side_effect=httpx.ConnectError("boom")
        )
        resp = await client.post(
            "/api/audit", json={"url": "https://unreachable.test/"}
        )
        assert resp.status_code == 502
        assert resp.json()["error_code"] == "unreachable_url"

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_returns_504(self, client):
        respx.get("https://slow.test/").mock(
            side_effect=httpx.TimeoutException("too slow")
        )
        resp = await client.post("/api/audit", json={"url": "https://slow.test/"})
        assert resp.status_code == 504
        assert resp.json()["error_code"] == "fetch_timeout"

    @pytest.mark.asyncio
    @respx.mock
    async def test_non_html_content_type_returns_415(self, client):
        respx.get("https://api.test/data.json").mock(
            return_value=httpx.Response(
                200, json={"ok": True}, headers={"content-type": "application/json"}
            )
        )
        resp = await client.post(
            "/api/audit", json={"url": "https://api.test/data.json"}
        )
        assert resp.status_code == 415
        assert resp.json()["error_code"] == "unsupported_content_type"

    @pytest.mark.asyncio
    @respx.mock
    async def test_upstream_404_returns_502(self, client):
        respx.get("https://gone.test/").mock(
            return_value=httpx.Response(404, text="not found")
        )
        resp = await client.post("/api/audit", json={"url": "https://gone.test/"})
        assert resp.status_code == 502
        assert resp.json()["error_code"] == "upstream_http_error"

    @pytest.mark.asyncio
    @respx.mock
    async def test_upstream_403_returns_502(self, client):
        """403 (e.g. LeetCode, Cloudflare) is a permanent 4xx — not retried."""
        respx.get("https://blocked.test/").mock(
            return_value=httpx.Response(403, text="forbidden")
        )
        resp = await client.post("/api/audit", json={"url": "https://blocked.test/"})
        assert resp.status_code == 502
        assert resp.json()["error_code"] == "upstream_http_error"

    @pytest.mark.asyncio
    @respx.mock
    async def test_pdf_content_type_returns_415(self, client):
        respx.get("https://files.test/doc.pdf").mock(
            return_value=httpx.Response(
                200, content=b"%PDF", headers={"content-type": "application/pdf"}
            )
        )
        resp = await client.post(
            "/api/audit", json={"url": "https://files.test/doc.pdf"}
        )
        assert resp.status_code == 415

    @pytest.mark.asyncio
    @respx.mock
    async def test_error_response_always_has_message_field(self, client):
        """Every error — regardless of type — must include a human-readable message."""
        respx.get("https://slow.test/").mock(side_effect=httpx.TimeoutException("slow"))
        body = (
            await client.post("/api/audit", json={"url": "https://slow.test/"})
        ).json()
        assert "message" in body
        assert len(body["message"]) > 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_xhtml_content_type_accepted(self, client):
        """application/xhtml+xml should be treated as valid HTML."""
        respx.get("https://xhtml.test/").mock(
            return_value=httpx.Response(
                200,
                text=MINIMAL_HTML,
                headers={"content-type": "application/xhtml+xml"},
            )
        )
        resp = await client.post("/api/audit", json={"url": "https://xhtml.test/"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Liveness probe
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_returns_200(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_health_returns_correct_body(self, client):
        assert (await client.get("/api/health")).json() == {"status": "ok"}

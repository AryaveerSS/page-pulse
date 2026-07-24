"""
Tests for the /api/audit endpoint itself, using respx to mock the outbound
fetch. This is what verifies the fetcher/router/exception-handler wiring -
i.e. that a real failure mode actually produces the HTTP status and error
body we designed for it, not just that the parser function is correct.
"""
import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.main import app

VALID_HTML = "<html><head><title>Test Page</title></head><body><h1>Hi</h1></body></html>"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
@respx.mock
async def test_audit_happy_path(client):
    respx.get("https://good-site.test/").mock(
        return_value=httpx.Response(200, text=VALID_HTML, headers={"content-type": "text/html"})
    )

    resp = await client.post("/api/audit", json={"url": "https://good-site.test/"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Test Page"
    assert body["h1_count"] == 1
    assert body["http_status"] == 200


@pytest.mark.asyncio
async def test_audit_rejects_invalid_url_with_422(client):
    resp = await client.post("/api/audit", json={"url": "not-a-url"})

    assert resp.status_code == 422
    assert resp.json()["error_code"] == "validation_error"


@pytest.mark.asyncio
@respx.mock
async def test_audit_maps_connection_error_to_502(client):
    respx.get("https://unreachable.test/").mock(side_effect=httpx.ConnectError("boom"))

    resp = await client.post("/api/audit", json={"url": "https://unreachable.test/"})

    assert resp.status_code == 502
    assert resp.json()["error_code"] == "unreachable_url"


@pytest.mark.asyncio
@respx.mock
async def test_audit_maps_timeout_to_504(client):
    respx.get("https://slow.test/").mock(side_effect=httpx.TimeoutException("too slow"))

    resp = await client.post("/api/audit", json={"url": "https://slow.test/"})

    assert resp.status_code == 504
    assert resp.json()["error_code"] == "fetch_timeout"


@pytest.mark.asyncio
@respx.mock
async def test_audit_rejects_non_html_content_type_with_415(client):
    respx.get("https://api.test/data.json").mock(
        return_value=httpx.Response(200, json={"ok": True}, headers={"content-type": "application/json"})
    )

    resp = await client.post("/api/audit", json={"url": "https://api.test/data.json"})

    assert resp.status_code == 415
    assert resp.json()["error_code"] == "unsupported_content_type"


@pytest.mark.asyncio
@respx.mock
async def test_audit_surfaces_upstream_4xx_as_502(client):
    respx.get("https://gone.test/").mock(return_value=httpx.Response(404, text="not found"))

    resp = await client.post("/api/audit", json={"url": "https://gone.test/"})

    assert resp.status_code == 502
    assert resp.json()["error_code"] == "upstream_http_error"


@pytest.mark.asyncio
async def test_health_check(client):
    resp = await client.get("/api/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

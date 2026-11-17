"""Tests for the FastAPI application endpoints."""

import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport

from main import app, _cache, _trusted, NODE_AGENT_TOKEN


@pytest.fixture(autouse=True)
def reset_state():
    _cache.clear()
    _trusted.clear()
    yield
    _cache.clear()
    _trusted.clear()


@pytest.fixture
def transport():
    return ASGITransport(app=app)


@pytest.fixture
def agent_headers():
    if NODE_AGENT_TOKEN:
        return {"X-Node-Agent-Token": NODE_AGENT_TOKEN}
    return {"X-Node-Agent-Token": "test-token-for-ci"}


# ---------- Health ----------


@pytest.mark.anyio
async def test_health_returns_ok(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("ok", "degraded")
    assert "node_agent" in body
    assert "upstreams" in body
    assert "cache" in body
    assert body["cache"]["max_entries"] > 0


@pytest.mark.anyio
async def test_health_no_credentials_leaked(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/health")
    text = resp.text.lower()
    assert "password" not in text
    assert "token" not in text
    assert "secret" not in text
    assert "rpcuser" not in text


# ---------- Validators on public endpoints ----------


@pytest.mark.anyio
async def test_invalid_block_hash_rejected(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/block/not-a-hash")
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_invalid_txid_rejected(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/tx/not_a_valid_txid_at_all_1234567890abcdef")
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_invalid_height_rejected(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/block-height/abc")
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_height_overflow_rejected(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/block-height/99999999")
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_invalid_address_rejected(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/address/not-an-address")
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_invalid_address_utxo_rejected(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/address/xyz/utxo")
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_invalid_address_txs_rejected(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/address/xyz/txs")
    assert resp.status_code == 400


# ---------- Node agent auth ----------


@pytest.mark.anyio
async def test_node_agent_ingest_requires_auth(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/node-agent/ingest", json={"tip_height": 870000})
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_node_agent_tip_requires_auth(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/node-agent/tip", json={"height": 870000})
    assert resp.status_code == 401


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_node_agent_ingest_accepts_valid_token(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/node-agent/ingest",
            json={"tip_height": 870000, "tip_hash": "abc123"},
            headers=headers,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 2


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_node_agent_tip_push(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/node-agent/tip",
            json={"height": 870123, "hash": "a" * 64},
            headers=headers,
        )
    assert resp.status_code == 200
    assert _trusted.get_tip_height() == 870123


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_node_agent_bad_hash_rejected(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/node-agent/tip",
            json={"height": 870123, "hash": "too-short"},
            headers=headers,
        )
    assert resp.status_code == 400


# ---------- Source priority ----------


@pytest.mark.anyio
async def test_trusted_data_served_before_upstream(transport):
    _trusted.put("tip_height", 870999)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/blocks/tip/height")
    assert resp.status_code == 200
    assert resp.json() == 870999


# ---------- Tip status / conflict ----------


@pytest.mark.anyio
async def test_tip_status_no_conflict(transport):
    _trusted.put("tip_height", 870000)
    with patch("main._fetch_from_upstreams", new_callable=AsyncMock, return_value="870001"):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/tip/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "node_agent"
    assert body["authoritative"] == 870000


@pytest.mark.anyio
async def test_tip_status_conflict_returns_503(transport):
    _trusted.put("tip_height", 870000)
    with patch("main._fetch_from_upstreams", new_callable=AsyncMock, return_value="870010"):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/tip/status")
    assert resp.status_code == 503
    assert "conflict" in resp.json()["detail"].lower()


# ---------- CORS ----------


@pytest.mark.anyio
async def test_cors_allows_get(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.options(
            "/api/health",
            headers={
                "Origin": "https://api.thronoschain.org",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_cors_blocks_post_from_browser(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.options(
            "/api/health",
            headers={
                "Origin": "https://api.thronoschain.org",
                "Access-Control-Request-Method": "DELETE",
            },
        )
    allow = resp.headers.get("access-control-allow-methods", "")
    assert "DELETE" not in allow


# ---------- Cache bounded ----------


@pytest.mark.anyio
async def test_cache_respects_max_entries():
    c = _cache
    c.clear()
    for i in range(100):
        c.put(f"key_{i}", f"val_{i}", "upstream")
    assert c.size <= int(c._max_entries)

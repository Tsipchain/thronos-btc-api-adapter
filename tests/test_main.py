"""Tests for the FastAPI application endpoints."""

import time
from pathlib import Path
import tempfile
import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport

from main import app, _cache, _trusted, NODE_AGENT_TOKEN
from deposit_store import DepositStore, MAINNET_GENESIS_HASH
import main as main_module


@pytest.fixture(autouse=True)
def reset_state():
    _cache.clear()
    _trusted.clear()
    main_module._last_upstream_tip = None
    yield
    _cache.clear()
    _trusted.clear()
    main_module._last_upstream_tip = None


@pytest.fixture
def transport():
    return ASGITransport(app=app)


# ---------- Health ----------


@pytest.mark.anyio
async def test_health_returns_ok(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("ok", "degraded", "conflict")
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


@pytest.mark.anyio
async def test_health_shows_sequence(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/health")
    body = resp.json()
    assert "sequence" in body["node_agent"]


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
        resp = await ac.post(
            "/api/node-agent/ingest",
            json={"sequence": 1, "tip_height": 870000},
        )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_node_agent_tip_requires_auth(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/node-agent/tip", json={"sequence": 1, "height": 870000})
    assert resp.status_code == 401


# ---------- Node agent ingest schema ----------


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_ingest_accepts_valid_namespaces(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    payload = {
        "sequence": 1,
        "tip_height": 870000,
        "tip_hash": "a" * 64,
        "block_height:870000": "b" * 64,
    }
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/node-agent/ingest", json=payload, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 3


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_ingest_rejects_arbitrary_keys(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    payload = {
        "sequence": 1,
        "arbitrary_key": "should_be_rejected",
        "another_random": 42,
    }
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/node-agent/ingest", json=payload, headers=headers)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "arbitrary_key" in detail["rejected"]
    assert "another_random" in detail["rejected"]


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_ingest_validates_values(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    payload = {
        "sequence": 1,
        "tip_height": "not-a-number",
        "tip_hash": "too-short",
    }
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/node-agent/ingest", json=payload, headers=headers)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert len(detail["rejected"]) == 2


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_ingest_requires_sequence(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/node-agent/ingest",
            json={"tip_height": 870000},
            headers=headers,
        )
    assert resp.status_code == 400
    assert "sequence" in resp.json()["detail"].lower()


# ---------- Node agent tip endpoint ----------


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_tip_push_valid(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/node-agent/tip",
            json={"sequence": 1, "height": 870123, "hash": "a" * 64},
            headers=headers,
        )
    assert resp.status_code == 200
    assert _trusted.get_tip_height() == 870123


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_tip_push_requires_sequence(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/node-agent/tip",
            json={"height": 870000, "hash": "a" * 64},
            headers=headers,
        )
    assert resp.status_code == 400


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_tip_hash_64_non_hex_rejected(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/node-agent/tip",
            json={"sequence": 1, "height": 870000, "hash": "z" * 64},
            headers=headers,
        )
    assert resp.status_code == 400


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_tip_hash_short_rejected(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/node-agent/tip",
            json={"sequence": 1, "hash": "abcdef"},
            headers=headers,
        )
    assert resp.status_code == 400


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_tip_hash_long_rejected(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/node-agent/tip",
            json={"sequence": 1, "hash": "a" * 65},
            headers=headers,
        )
    assert resp.status_code == 400


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_tip_hash_mixed_invalid_rejected(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    bad_hash = "0123456789abcdef" * 3 + "GHIJKLMNOPQRSTUV"
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/node-agent/tip",
            json={"sequence": 1, "hash": bad_hash},
            headers=headers,
        )
    assert resp.status_code == 400


# ---------- Sequence / replay protection ----------


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_replay_rejected_on_ingest(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp1 = await ac.post(
            "/api/node-agent/ingest",
            json={"sequence": 5, "tip_height": 870000},
            headers=headers,
        )
        assert resp1.status_code == 200

        resp2 = await ac.post(
            "/api/node-agent/ingest",
            json={"sequence": 5, "tip_height": 870001},
            headers=headers,
        )
        assert resp2.status_code == 409

        resp3 = await ac.post(
            "/api/node-agent/ingest",
            json={"sequence": 3, "tip_height": 870002},
            headers=headers,
        )
        assert resp3.status_code == 409


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_replay_rejected_on_tip(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp1 = await ac.post(
            "/api/node-agent/tip",
            json={"sequence": 10, "height": 870000},
            headers=headers,
        )
        assert resp1.status_code == 200

        resp2 = await ac.post(
            "/api/node-agent/tip",
            json={"sequence": 10, "height": 870001},
            headers=headers,
        )
        assert resp2.status_code == 409


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_replay_does_not_refresh_trusted_data(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post(
            "/api/node-agent/ingest",
            json={"sequence": 5, "tip_height": 870000},
            headers=headers,
        )
        original_tip = _trusted.get_tip_height()
        assert original_tip == 870000

        resp = await ac.post(
            "/api/node-agent/ingest",
            json={"sequence": 3, "tip_height": 999999},
            headers=headers,
        )
        assert resp.status_code == 409
        assert _trusted.get_tip_height() == 870000


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_forward_sequence_accepted(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp1 = await ac.post(
            "/api/node-agent/tip",
            json={"sequence": 1, "height": 870000},
            headers=headers,
        )
        assert resp1.status_code == 200

        resp2 = await ac.post(
            "/api/node-agent/tip",
            json={"sequence": 2, "height": 870001},
            headers=headers,
        )
        assert resp2.status_code == 200
        assert _trusted.get_tip_height() == 870001


# ---------- Trusted data TTL ----------


@pytest.mark.anyio
async def test_trusted_data_does_not_extend_via_cache(transport):
    """Trusted data must expire per NODE_AGENT_STALE_SECONDS, not get
    re-cached with a fresh generic-cache timestamp."""
    s = TrustedStore(stale_seconds=0.05)
    s.put("tip_height", 870000)
    s.advance_sequence(1)

    entry = s.get("tip_height")
    assert entry is not None

    time.sleep(0.06)
    assert s.get("tip_height") is None


from cache import TrustedStore


@pytest.mark.anyio
async def test_resolve_does_not_recache_trusted(transport):
    """When _resolve returns trusted data, it should NOT put it
    in the generic cache."""
    _trusted.put("tip_height", 870555)
    _trusted.advance_sequence(1)

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/blocks/tip/height")
    assert resp.status_code == 200
    assert resp.json() == 870555

    assert _cache.get("tip_height") is None


# ---------- Source priority ----------


@pytest.mark.anyio
async def test_trusted_data_served_before_upstream(transport):
    _trusted.put("tip_height", 870999)
    _trusted.advance_sequence(1)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/blocks/tip/height")
    assert resp.status_code == 200
    assert resp.json() == 870999


# ---------- Conflict policy on reads ----------


@pytest.mark.anyio
async def test_tip_status_no_conflict(transport):
    _trusted.put("tip_height", 870000)
    _trusted.advance_sequence(1)
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
    _trusted.advance_sequence(1)
    with patch("main._fetch_from_upstreams", new_callable=AsyncMock, return_value="870010"):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/tip/status")
    assert resp.status_code == 503
    assert "conflict" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_conflict_blocks_block_height_read(transport):
    """When tip conflict exists, block-height read returns 503."""
    _trusted.put("tip_height", 870000)
    _trusted.advance_sequence(1)
    main_module._last_upstream_tip = 870020

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/block-height/870000")
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_conflict_blocks_block_read(transport):
    _trusted.put("tip_height", 870000)
    _trusted.advance_sequence(1)
    main_module._last_upstream_tip = 870020

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/block/" + "a" * 64)
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_conflict_blocks_tx_read(transport):
    _trusted.put("tip_height", 870000)
    _trusted.advance_sequence(1)
    main_module._last_upstream_tip = 870020

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/tx/" + "b" * 64)
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_conflict_blocks_tip_height_read(transport):
    _trusted.put("tip_height", 870000)
    _trusted.advance_sequence(1)
    main_module._last_upstream_tip = 870020

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/blocks/tip/height")
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_no_conflict_when_only_upstream(transport):
    """When no trusted tip, there is no conflict — upstream serves normally."""
    main_module._last_upstream_tip = 870000
    with patch("main._fetch_from_upstreams", new_callable=AsyncMock, return_value="870000"):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/blocks/tip/height")
    assert resp.status_code == 200


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



@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_invalid_ingest_does_not_consume_sequence(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        bad = await ac.post(
            "/api/node-agent/ingest",
            json={"sequence": 5, "tip_hash": "bad"},
            headers=headers,
        )
        assert bad.status_code == 400
        assert _trusted.sequence == 0

        good = await ac.post(
            "/api/node-agent/ingest",
            json={"sequence": 5, "tip_height": 870000},
            headers=headers,
        )
        assert good.status_code == 200
        assert _trusted.sequence == 5
        assert _trusted.get_tip_height() == 870000


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_invalid_tip_does_not_consume_sequence(transport):
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        bad = await ac.post(
            "/api/node-agent/tip",
            json={"sequence": 8, "height": -1, "hash": "a" * 64},
            headers=headers,
        )
        assert bad.status_code == 400
        assert _trusted.sequence == 0

        good = await ac.post(
            "/api/node-agent/tip",
            json={"sequence": 8, "height": 870000, "hash": "a" * 64},
            headers=headers,
        )
        assert good.status_code == 200
        assert _trusted.sequence == 8


def _deposit_payload():
    txid = "11" * 32
    return {
        "network": "main",
        "genesis_hash": MAINNET_GENESIS_HASH,
        "event_id": f"main:{txid}:0",
        "idempotency_key": f"main:{txid}:0",
        "txid": txid,
        "vout": 0,
        "address": "bc1qtestaddress000000000000000000000000000",
        "owner_ref": "bridge-intent-1",
        "amount_sats": 25000,
        "confirmations": 6,
        "minimum_confirmations": 6,
        "block_hash": "aa" * 32,
        "block_height": 976310,
        "requires_reconciliation": False,
    }


@pytest.mark.anyio
async def test_deposit_ingest_requires_auth(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/node-agent/deposits", json=_deposit_payload())
    assert resp.status_code == 401


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_deposit_ingest_fails_closed_when_not_configured(transport, monkeypatch):
    monkeypatch.setattr(main_module, "_deposit_store", None)
    headers = {"X-Node-Agent-Token": "test-secret-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/node-agent/deposits", json=_deposit_payload(), headers=headers)
    assert resp.status_code == 503


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_deposit_ingest_is_durable_and_idempotent(transport, monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        store = DepositStore(str(Path(td) / "deposits.db"))
        monkeypatch.setattr(main_module, "_deposit_store", store)
        headers = {"X-Node-Agent-Token": "test-secret-token"}
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            first = await ac.post("/api/node-agent/deposits", json=_deposit_payload(), headers=headers)
            second = await ac.post("/api/node-agent/deposits", json=_deposit_payload(), headers=headers)
        assert first.status_code == 200
        assert first.json()["status"] == "accepted"
        assert first.json()["credit_applied"] is False
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"
        assert store.count() == 1


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_deposit_ingest_rejects_nonfinal_and_network_mismatch(transport, monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        store = DepositStore(str(Path(td) / "deposits.db"))
        monkeypatch.setattr(main_module, "_deposit_store", store)
        headers = {"X-Node-Agent-Token": "test-secret-token"}
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            nonfinal = _deposit_payload()
            nonfinal["confirmations"] = 5
            resp = await ac.post("/api/node-agent/deposits", json=nonfinal, headers=headers)
            assert resp.status_code == 409

            wrong_network = _deposit_payload()
            wrong_network["network"] = "test"
            resp = await ac.post("/api/node-agent/deposits", json=wrong_network, headers=headers)
            assert resp.status_code == 400
        assert store.count() == 0


@pytest.mark.anyio
@patch("main.NODE_AGENT_TOKEN", "test-secret-token")
async def test_deposit_idempotency_conflict_returns_409(transport, monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        store = DepositStore(str(Path(td) / "deposits.db"))
        monkeypatch.setattr(main_module, "_deposit_store", store)
        headers = {"X-Node-Agent-Token": "test-secret-token"}
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            first = await ac.post("/api/node-agent/deposits", json=_deposit_payload(), headers=headers)
            assert first.status_code == 200
            changed = _deposit_payload()
            changed["amount_sats"] = 25001
            conflict = await ac.post("/api/node-agent/deposits", json=changed, headers=headers)
            assert conflict.status_code == 409
        assert store.count() == 1

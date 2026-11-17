"""
Thronos BTC API Adapter.

Blockstream-compatible Bitcoin API proxy with:
- Node-agent trusted data ingest (push from private bitcoind observer)
- Source priority: node_agent > cache > external upstreams
- Bounded LRU cache with provenance
- Input validation on all path parameters and ingest payloads
- Conflict detection between trusted and upstream sources
- Monotonic sequence enforcement on node-agent updates
- Shared HTTP client pool
"""

import os
import time
import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Union

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from cache import BoundedCache, TrustedStore, SOURCE_NODE_AGENT, SOURCE_UPSTREAM
from deposit_store import DepositStore, DepositStoreError, MAINNET_GENESIS_HASH
from validators import (
    validate_address,
    validate_block_hash,
    validate_block_hash_value,
    validate_height,
    validate_txid,
    validate_ingest_key,
    validate_ingest_value,
    INGEST_MAX_BODY_BYTES,
    INGEST_MAX_ITEMS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

UPSTREAMS: List[str] = [
    u.rstrip("/")
    for u in os.getenv("UPSTREAMS", "https://blockstream.info/api").split(",")
    if u.strip()
]
CACHE_TTL: int = int(os.getenv("CACHE_TTL", "30"))
CACHE_MAX_ENTRIES: int = int(os.getenv("CACHE_MAX_ENTRIES", "10000"))
RATE_LIMIT_RPS: float = float(os.getenv("RATE_LIMIT_RPS", "5"))

NODE_AGENT_TOKEN: str = os.getenv("NODE_AGENT_TOKEN", "")
NODE_AGENT_STALE_SECONDS: float = float(os.getenv("NODE_AGENT_STALE_SECONDS", "120"))
TRUSTED_MAX_ENTRIES: int = int(os.getenv("TRUSTED_MAX_ENTRIES", "5000"))

TIP_CONFLICT_THRESHOLD: int = int(os.getenv("TIP_CONFLICT_THRESHOLD", "2"))

BTC_DEPOSIT_DB_PATH: str = os.getenv("BTC_DEPOSIT_DB_PATH", "").strip()
BTC_DEPOSIT_MIN_CONFIRMATIONS: int = int(
    os.getenv("BTC_DEPOSIT_MIN_CONFIRMATIONS", "6")
)
if BTC_DEPOSIT_MIN_CONFIRMATIONS < 1 or BTC_DEPOSIT_MIN_CONFIRMATIONS > 10_000:
    raise RuntimeError("BTC_DEPOSIT_MIN_CONFIRMATIONS out of range")

CORS_ORIGINS: List[str] = os.getenv(
    "CORS_ORIGINS",
    "https://api.thronoschain.org,https://commerce.thronoschain.org,https://explorer.thronoschain.org",
).split(",")

MAX_RESPONSE_BYTES: int = 16 * 1024 * 1024  # 16 MB

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_cache = BoundedCache(max_entries=CACHE_MAX_ENTRIES, ttl=CACHE_TTL)
_trusted = TrustedStore(stale_seconds=NODE_AGENT_STALE_SECONDS, max_entries=TRUSTED_MAX_ENTRIES)
_deposit_store: Optional[DepositStore] = (
    DepositStore(BTC_DEPOSIT_DB_PATH) if BTC_DEPOSIT_DB_PATH else None
)

_http_client: Optional[httpx.AsyncClient] = None
_last_request_time: float = 0.0
_rate_lock = asyncio.Lock()

_start_time: float = time.time()

_last_upstream_tip: Optional[int] = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"User-Agent": "ThronosBTCAdapter/2.0"},
        )
    return _http_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()


app = FastAPI(title="Thronos BTC API Adapter", version="2.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Node-Agent-Token"],
)

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _verify_node_agent_token(token: str) -> bool:
    if not NODE_AGENT_TOKEN:
        return False
    return hmac.compare_digest(token, NODE_AGENT_TOKEN)


async def require_node_agent(
    x_node_agent_token: Optional[str] = Header(None, alias="X-Node-Agent-Token"),
    authorization: Optional[str] = Header(None),
) -> None:
    token = x_node_agent_token
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token or not _verify_node_agent_token(token):
        raise HTTPException(status_code=401, detail="Invalid or missing node agent token")


def _strict_nonnegative_int(value: Any, field: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=400, detail=f"{field} must be an integer")
    if value < 0 or value > maximum:
        raise HTTPException(status_code=400, detail=f"{field} out of range")
    return value


def _strict_positive_int(value: Any, field: str, *, maximum: int) -> int:
    parsed = _strict_nonnegative_int(value, field, maximum=maximum)
    if parsed <= 0:
        raise HTTPException(status_code=400, detail=f"{field} must be positive")
    return parsed


def _bounded_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{field} must be a string")
    result = value.strip()
    if not result or len(result) > maximum:
        raise HTTPException(status_code=400, detail=f"{field} invalid")
    return result


def _validate_deposit_payload(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    network = _bounded_text(body.get("network"), "network", 16)
    if network != "main":
        raise HTTPException(status_code=400, detail="Only Bitcoin mainnet deposits are accepted")

    genesis_hash = validate_block_hash_value(body.get("genesis_hash"))
    if genesis_hash != MAINNET_GENESIS_HASH:
        raise HTTPException(status_code=400, detail="Bitcoin network identity mismatch")

    txid = validate_block_hash_value(body.get("txid"))
    if txid is None:
        raise HTTPException(status_code=400, detail="txid must be exactly 64 hex characters")

    vout = _strict_nonnegative_int(body.get("vout"), "vout", maximum=10_000_000)
    amount_sats = _strict_positive_int(
        body.get("amount_sats"), "amount_sats", maximum=2_100_000_000_000_000
    )
    confirmations = _strict_nonnegative_int(
        body.get("confirmations"), "confirmations", maximum=10_000_000
    )
    minimum_confirmations = _strict_positive_int(
        body.get("minimum_confirmations"), "minimum_confirmations", maximum=10_000
    )
    if minimum_confirmations < BTC_DEPOSIT_MIN_CONFIRMATIONS:
        raise HTTPException(
            status_code=409,
            detail="Deposit confirmation policy is below the adapter minimum",
        )
    if confirmations < minimum_confirmations:
        raise HTTPException(status_code=409, detail="Deposit is not final")

    block_hash = validate_block_hash_value(body.get("block_hash"))
    if block_hash is None:
        raise HTTPException(status_code=400, detail="block_hash must be exactly 64 hex characters")
    block_height = _strict_nonnegative_int(
        body.get("block_height"), "block_height", maximum=10_000_000
    )

    address = _bounded_text(body.get("address"), "address", 128)
    owner_ref = _bounded_text(body.get("owner_ref"), "owner_ref", 200)

    if body.get("requires_reconciliation") is not False:
        raise HTTPException(status_code=409, detail="Deposit requires reconciliation")

    event_id = _bounded_text(body.get("event_id"), "event_id", 160)
    expected_event_id = f"{network}:{txid}:{vout}"
    if event_id != expected_event_id:
        raise HTTPException(status_code=400, detail="event_id does not match network:txid:vout")

    idempotency_key = _bounded_text(
        body.get("idempotency_key"), "idempotency_key", 160
    )
    if idempotency_key != expected_event_id:
        raise HTTPException(status_code=400, detail="idempotency_key mismatch")

    return {
        "network": network,
        "genesis_hash": genesis_hash,
        "event_id": event_id,
        "idempotency_key": idempotency_key,
        "txid": txid,
        "vout": vout,
        "address": address,
        "owner_ref": owner_ref,
        "amount_sats": amount_sats,
        "confirmations": confirmations,
        "minimum_confirmations": minimum_confirmations,
        "block_hash": block_hash,
        "block_height": block_height,
        "requires_reconciliation": False,
    }


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


async def _rate_limit() -> None:
    global _last_request_time
    if RATE_LIMIT_RPS <= 0:
        return
    async with _rate_lock:
        now = time.time()
        min_interval = 1.0 / RATE_LIMIT_RPS
        wait_time = min_interval - (now - _last_request_time)
        if wait_time > 0:
            await asyncio.sleep(wait_time)
        _last_request_time = time.time()


# ---------------------------------------------------------------------------
# Conflict policy
# ---------------------------------------------------------------------------


def _check_tip_conflict(trusted_tip: Optional[int], upstream_tip: Optional[int]) -> Optional[str]:
    if trusted_tip is None or upstream_tip is None:
        return None
    diff = abs(trusted_tip - upstream_tip)
    if diff > TIP_CONFLICT_THRESHOLD:
        return f"Tip height conflict: node_agent={trusted_tip}, upstream={upstream_tip}, delta={diff}"
    return None


def _enforce_conflict_policy() -> None:
    """Raise 503 if trusted and upstream tips are in conflict.

    Called by every chain-data read endpoint so conflicting state
    is never silently served.
    """
    trusted_tip = _trusted.get_tip_height()
    conflict = _check_tip_conflict(trusted_tip, _last_upstream_tip)
    if conflict:
        raise HTTPException(status_code=503, detail=conflict)


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------


async def _fetch_from_upstreams(path: str) -> Any:
    global _last_upstream_tip
    client = _get_http_client()
    for base in UPSTREAMS:
        url = f"{base}{path}"
        try:
            await _rate_limit()
            resp = await client.get(url)
            if resp.status_code == 200:
                if len(resp.content) > MAX_RESPONSE_BYTES:
                    logger.warning("Upstream %s response too large (%d bytes)", url, len(resp.content))
                    continue
                content_type = resp.headers.get("content-type", "")
                if "application/json" in content_type:
                    result = resp.json()
                else:
                    result = resp.text

                if path == "/blocks/tip/height":
                    try:
                        _last_upstream_tip = int(result if isinstance(result, str) else str(result))
                    except (ValueError, TypeError):
                        pass

                return result
        except Exception as exc:
            logger.warning("Upstream %s failed: %s", url, exc)
            continue
    raise HTTPException(status_code=502, detail="All upstreams failed")


async def _resolve(cache_key: str, upstream_path: str) -> Any:
    """Source priority: trusted node-agent > cache > upstream fallback.

    Trusted entries are served directly without being copied into the
    generic cache, so their lifetime is governed solely by
    NODE_AGENT_STALE_SECONDS.
    """
    _enforce_conflict_policy()

    trusted_entry = _trusted.get(cache_key)
    if trusted_entry is not None:
        return trusted_entry.value

    cached = _cache.get(cache_key)
    if cached is not None:
        return cached.value

    result = await _fetch_from_upstreams(upstream_path)
    _cache.put(cache_key, result, SOURCE_UPSTREAM)
    return result


# ---------------------------------------------------------------------------
# Node Agent ingest endpoints (POST, authenticated)
# ---------------------------------------------------------------------------


@app.post("/api/node-agent/ingest", dependencies=[Depends(require_node_agent)])
async def node_agent_ingest(request: Request) -> Dict[str, Any]:
    """Accept a fully validated node-agent batch without sequence poisoning."""
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > INGEST_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Body exceeds {INGEST_MAX_BODY_BYTES} bytes")

    raw = await request.body()
    if len(raw) > INGEST_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Body exceeds {INGEST_MAX_BODY_BYTES} bytes")
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    seq = body.pop("sequence", None)
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise HTTPException(status_code=400, detail="sequence must be an integer")
    if seq <= 0:
        raise HTTPException(status_code=400, detail="sequence out of range")
    if len(body) > INGEST_MAX_ITEMS:
        raise HTTPException(status_code=400, detail=f"Batch exceeds {INGEST_MAX_ITEMS} items")

    validated: Dict[str, Any] = {}
    rejected_keys: list[str] = []
    for key, value in body.items():
        normalized_key = validate_ingest_key(key)
        if normalized_key is None:
            rejected_keys.append(str(key))
            continue
        valid, normalized_value = validate_ingest_value(normalized_key, value)
        if not valid:
            rejected_keys.append(str(key))
            continue
        validated[normalized_key] = normalized_value

    if rejected_keys:
        raise HTTPException(
            status_code=400,
            detail={"message": "Invalid ingest batch", "rejected": rejected_keys},
        )

    if not _trusted.commit(seq, validated):
        raise HTTPException(
            status_code=409,
            detail=f"Stale sequence {seq}; current high-water mark is {_trusted.sequence}",
        )
    return {"accepted": len(validated), "timestamp": time.time()}


@app.post("/api/node-agent/tip", dependencies=[Depends(require_node_agent)])
async def node_agent_push_tip(request: Request) -> Dict[str, Any]:
    """Accept a validated tip atomically with its monotonic sequence."""
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > INGEST_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Body exceeds {INGEST_MAX_BODY_BYTES} bytes")

    raw = await request.body()
    if len(raw) > INGEST_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Body exceeds {INGEST_MAX_BODY_BYTES} bytes")
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    seq = body.get("sequence")
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise HTTPException(status_code=400, detail="sequence must be an integer")
    if seq <= 0:
        raise HTTPException(status_code=400, detail="sequence out of range")

    items: Dict[str, Any] = {}
    height = body.get("height")
    block_hash = body.get("hash")

    if height is not None:
        height = _strict_nonnegative_int(height, "height", maximum=10_000_000)
        items["tip_height"] = height

    if block_hash is not None:
        normalized = validate_block_hash_value(block_hash)
        if normalized is None:
            raise HTTPException(status_code=400, detail="hash must be exactly 64 hex characters")
        items["tip_hash"] = normalized
        if height is not None:
            items[f"block_height:{height}"] = normalized

    if not items:
        raise HTTPException(status_code=400, detail="Tip payload contains no chain data")

    if not _trusted.commit(seq, items):
        raise HTTPException(
            status_code=409,
            detail=f"Stale sequence {seq}; current high-water mark is {_trusted.sequence}",
        )
    return {"accepted": True, "timestamp": time.time()}


@app.post("/api/node-agent/deposits", dependencies=[Depends(require_node_agent)])
async def node_agent_push_deposit(request: Request) -> Dict[str, Any]:
    """Durably accept a confirmed deposit observation, without crediting balances."""
    if _deposit_store is None:
        raise HTTPException(status_code=503, detail="Deposit ingest is not configured")

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > INGEST_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Body exceeds {INGEST_MAX_BODY_BYTES} bytes")
    raw = await request.body()
    if len(raw) > INGEST_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Body exceeds {INGEST_MAX_BODY_BYTES} bytes")
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON")

    payload = _validate_deposit_payload(body)
    try:
        result = _deposit_store.accept(payload)
    except DepositStoreError as exc:
        if str(exc) == "deposit_idempotency_conflict":
            raise HTTPException(status_code=409, detail="Deposit idempotency conflict") from exc
        raise HTTPException(status_code=500, detail="Deposit persistence failed") from exc

    return {
        "accepted": True,
        "status": result.status,
        "idempotency_key": result.idempotency_key,
        "received_at": result.received_at,
        "credit_applied": False,
    }


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    global _last_upstream_tip
    upstreams_reachable = 0
    upstream_tip = None

    for base in UPSTREAMS:
        try:
            client = _get_http_client()
            resp = await client.get(f"{base}/blocks/tip/height", timeout=5.0)
            if resp.status_code == 200:
                upstreams_reachable += 1
                if upstream_tip is None:
                    try:
                        upstream_tip = int(resp.text.strip())
                        _last_upstream_tip = upstream_tip
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass

    trusted_tip = _trusted.get_tip_height()
    conflict = _check_tip_conflict(trusted_tip, upstream_tip)

    status = "ok"
    if conflict:
        status = "conflict"
    elif not _trusted.is_connected and upstreams_reachable == 0:
        status = "degraded"

    result: Dict[str, Any] = {
        "status": status,
        "uptime_seconds": int(time.time() - _start_time),
        "node_agent": {
            "connected": _trusted.is_connected,
            "tip_height": trusted_tip,
            "sequence": _trusted.sequence,
        },
        "upstreams": {
            "reachable": upstreams_reachable,
            "total": len(UPSTREAMS),
            "tip_height": upstream_tip,
        },
        "cache": {
            "entries": _cache.size,
            "max_entries": CACHE_MAX_ENTRIES,
            "ttl_seconds": CACHE_TTL,
        },
        "deposit_ingest": {
            "configured": _deposit_store is not None,
            "accepted_events": _deposit_store.count() if _deposit_store is not None else 0,
            "credit_enabled": False,
        },
    }

    if conflict:
        result["conflict"] = conflict

    return result


# ---------------------------------------------------------------------------
# Public API endpoints (Blockstream-compatible)
# ---------------------------------------------------------------------------


@app.get("/api/blocks/tip/height")
async def get_tip_height() -> Union[int, str]:
    _enforce_conflict_policy()

    trusted_tip = _trusted.get_tip_height()
    if trusted_tip is not None:
        return trusted_tip

    data = await _resolve("tip_height", "/blocks/tip/height")
    try:
        return int(data)
    except (ValueError, TypeError):
        return data


@app.get("/api/block-height/{height}")
async def get_block_hash(height: str) -> Any:
    height = validate_height(height)
    return await _resolve(f"block_height:{height}", f"/block-height/{height}")


@app.get("/api/block/{block_hash}")
async def get_block(block_hash: str) -> Any:
    block_hash = validate_block_hash(block_hash)
    return await _resolve(f"block:{block_hash}", f"/block/{block_hash}")


@app.get("/api/tx/{txid}")
async def get_transaction(txid: str) -> Any:
    txid = validate_txid(txid)
    return await _resolve(f"tx:{txid}", f"/tx/{txid}")


@app.get("/api/address/{address}/utxo")
async def get_utxo(address: str) -> Any:
    address = validate_address(address)
    return await _resolve(f"utxo:{address}", f"/address/{address}/utxo")


@app.get("/api/address/{address}")
async def get_address(address: str) -> Any:
    address = validate_address(address)
    return await _resolve(f"address:{address}", f"/address/{address}")


@app.get("/api/address/{address}/txs")
async def get_address_txs(address: str) -> Any:
    address = validate_address(address)
    return await _resolve(f"address_txs:{address}", f"/address/{address}/txs")


@app.get("/api/address/{address}/txs/mempool")
async def get_address_mempool_txs(address: str) -> Any:
    address = validate_address(address)
    return await _resolve(f"address_mempool:{address}", f"/address/{address}/txs/mempool")


# ---------------------------------------------------------------------------
# Conflict-aware tip endpoint
# ---------------------------------------------------------------------------


@app.get("/api/tip/status")
async def get_tip_status() -> Dict[str, Any]:
    """Return tip height from all sources with conflict detection."""
    trusted_tip = _trusted.get_tip_height()

    upstream_tip = None
    try:
        data = await _fetch_from_upstreams("/blocks/tip/height")
        upstream_tip = int(data)
    except Exception:
        pass

    conflict = _check_tip_conflict(trusted_tip, upstream_tip)

    if conflict:
        raise HTTPException(status_code=503, detail=conflict)

    return {
        "node_agent": trusted_tip,
        "upstream": upstream_tip,
        "authoritative": trusted_tip if trusted_tip is not None else upstream_tip,
        "source": "node_agent" if trusted_tip is not None else "upstream",
    }


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host=host, port=port, reload=False)

"""
Thronos BTC API Adapter.

Blockstream-compatible Bitcoin API proxy with:
- Node-agent trusted data ingest (push from private bitcoind observer)
- Source priority: node_agent > cache > external upstreams
- Bounded LRU cache with provenance
- Input validation on all path parameters
- Conflict detection between trusted and upstream sources
- Shared HTTP client pool
"""

import os
import time
import asyncio
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Union

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from cache import BoundedCache, TrustedStore, SOURCE_NODE_AGENT, SOURCE_UPSTREAM
from validators import validate_address, validate_block_hash, validate_height, validate_txid

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

TIP_CONFLICT_THRESHOLD: int = int(os.getenv("TIP_CONFLICT_THRESHOLD", "2"))

CORS_ORIGINS: List[str] = os.getenv(
    "CORS_ORIGINS",
    "https://api.thronoschain.org,https://commerce.thronoschain.org,https://explorer.thronoschain.org",
).split(",")

MAX_RESPONSE_BYTES: int = 16 * 1024 * 1024  # 16 MB

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_cache = BoundedCache(max_entries=CACHE_MAX_ENTRIES, ttl=CACHE_TTL)
_trusted = TrustedStore(stale_seconds=NODE_AGENT_STALE_SECONDS)

_http_client: Optional[httpx.AsyncClient] = None
_last_request_time: float = 0.0
_rate_lock = asyncio.Lock()

_start_time: float = time.time()


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


app = FastAPI(title="Thronos BTC API Adapter", version="2.0.0", lifespan=lifespan)

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
# Source resolution
# ---------------------------------------------------------------------------


async def _fetch_from_upstreams(path: str) -> Any:
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
                    return resp.json()
                else:
                    return resp.text
        except Exception as exc:
            logger.warning("Upstream %s failed: %s", url, exc)
            continue
    raise HTTPException(status_code=502, detail="All upstreams failed")


async def _resolve(cache_key: str, upstream_path: str) -> Any:
    """Source priority: trusted node-agent > cache > upstream fallback."""
    trusted_entry = _trusted.get(cache_key)
    if trusted_entry is not None:
        _cache.put(cache_key, trusted_entry.value, SOURCE_NODE_AGENT)
        return trusted_entry.value

    cached = _cache.get(cache_key)
    if cached is not None:
        return cached.value

    result = await _fetch_from_upstreams(upstream_path)
    _cache.put(cache_key, result, SOURCE_UPSTREAM)
    return result


def _check_tip_conflict(trusted_tip: Optional[int], upstream_tip: Optional[int]) -> Optional[str]:
    if trusted_tip is None or upstream_tip is None:
        return None
    diff = abs(trusted_tip - upstream_tip)
    if diff > TIP_CONFLICT_THRESHOLD:
        return f"Tip height conflict: node_agent={trusted_tip}, upstream={upstream_tip}, delta={diff}"
    return None


# ---------------------------------------------------------------------------
# Node Agent ingest endpoints (POST, authenticated)
# ---------------------------------------------------------------------------


@app.post("/api/node-agent/ingest", dependencies=[Depends(require_node_agent)])
async def node_agent_ingest(request: Request) -> Dict[str, Any]:
    """Accept a batch of verified chain data from the node agent."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    accepted = 0
    for key, value in body.items():
        if not isinstance(key, str) or len(key) > 256:
            continue
        _trusted.put(key, value)
        accepted += 1

    return {"accepted": accepted, "timestamp": time.time()}


@app.post("/api/node-agent/tip", dependencies=[Depends(require_node_agent)])
async def node_agent_push_tip(request: Request) -> Dict[str, Any]:
    """Accept tip height + hash from node agent. Triggers conflict check."""
    body = await request.json()
    height = body.get("height")
    block_hash = body.get("hash")

    if height is not None:
        try:
            height = int(height)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="height must be an integer")
        _trusted.put("tip_height", height)

    if block_hash is not None:
        if not isinstance(block_hash, str) or len(block_hash) != 64:
            raise HTTPException(status_code=400, detail="hash must be 64 hex characters")
        _trusted.put("tip_hash", block_hash.lower())
        if height is not None:
            _trusted.put(f"block_height:{height}", block_hash.lower())

    return {"accepted": True, "timestamp": time.time()}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    upstreams_reachable = 0
    for base in UPSTREAMS:
        try:
            client = _get_http_client()
            resp = await client.get(f"{base}/blocks/tip/height", timeout=5.0)
            if resp.status_code == 200:
                upstreams_reachable += 1
        except Exception:
            pass

    trusted_tip = _trusted.get_tip_height()
    upstream_tip = None
    try:
        for base in UPSTREAMS:
            try:
                client = _get_http_client()
                resp = await client.get(f"{base}/blocks/tip/height", timeout=5.0)
                if resp.status_code == 200:
                    upstream_tip = int(resp.text.strip())
                    break
            except Exception:
                continue
    except Exception:
        pass

    conflict = _check_tip_conflict(trusted_tip, upstream_tip)

    result: Dict[str, Any] = {
        "status": "degraded" if conflict else "ok",
        "uptime_seconds": int(time.time() - _start_time),
        "node_agent": {
            "connected": _trusted.is_connected,
            "tip_height": trusted_tip,
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
    }

    if conflict:
        result["conflict"] = conflict

    return result


# ---------------------------------------------------------------------------
# Public API endpoints (Blockstream-compatible)
# ---------------------------------------------------------------------------


@app.get("/api/blocks/tip/height")
async def get_tip_height() -> Union[int, str]:
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

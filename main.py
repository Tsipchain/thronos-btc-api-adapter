"""
Thronos BTC API Adapter.

This module defines a small FastAPI application that proxies calls to one
or more external Bitcoin API providers (e.g. blockstream.info or
mempool.space) and exposes a simplified API under the `/api` prefix.
It provides a health check, latest block height, block lookup, tx
lookup, and address lookup endpoints.  Responses are cached in
memory for a short period to reduce load on upstream providers and
rate limiting is applied to protect against excessive requests.

To run the application locally:

```
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Configure upstream providers and other settings via environment variables
in a `.env` file or the process environment.  See README.md for
details.
"""

import os
import time
import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)

app = FastAPI(title="Thronos BTC API Adapter", version="0.1.0")

# Read configuration from environment variables
UPSTREAMS: List[str] = [u.rstrip("/") for u in os.getenv("UPSTREAMS", "https://blockstream.info/api").split(",") if u.strip()]
CACHE_TTL: int = int(os.getenv("CACHE_TTL", "30"))
RATE_LIMIT_RPS: float = float(os.getenv("RATE_LIMIT_RPS", "5"))

# Allow all origins by default for CORS; adjust as needed when deploying
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# In-memory cache structure: maps (endpoint_name, args, kwargs) -> (result, timestamp)
_cache: Dict[Tuple[str, Tuple[Any, ...], Tuple[Tuple[str, Any], ...]], Tuple[Any, float]] = {}

_last_request_time: float = 0.0
_rate_lock = asyncio.Lock()


async def _rate_limit() -> None:
    """Simple asynchronous rate limiter shared across all upstream calls."""
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


async def _fetch_from_upstreams(path: str) -> Any:
    """
    Attempt to fetch data from each configured upstream in order.  Returns
    the data from the first successful upstream.  Raises HTTPException
    if all upstreams fail.

    Args:
        path: The path portion of the request to append to each upstream
            base URL.  Must include the leading slash (e.g. '/block/abc...').

    Returns:
        The deserialized JSON data if the upstream returns JSON, or
        the raw text body otherwise.  Caller must serialize the
        response appropriately.
    """
    headers = {"User-Agent": "ThronosBTCAdapter/1.0"}
    for base in UPSTREAMS:
        url = f"{base}{path}"
        try:
            await _rate_limit()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "")
                if "application/json" in content_type:
                    return resp.json()
                else:
                    return resp.text
        except Exception as exc:
            logger.warning("Upstream %s failed: %s", url, exc)
            continue
    raise HTTPException(status_code=502, detail="All upstreams failed")


def _cache_key(name: str, *args: Any, **kwargs: Any) -> Tuple[str, Tuple[Any, ...], Tuple[Tuple[str, Any], ...]]:
    return (name, args, tuple(sorted(kwargs.items())))


async def _get_cached_or_fetch(name: str, path_func, *args: Any, **kwargs: Any) -> Any:
    """
    Helper to implement caching for endpoint calls.  Computes a cache
    key from the endpoint name and arguments.  If a recent entry
    exists, returns it.  Otherwise fetches from upstream and stores
    the result with a timestamp.
    """
    key = _cache_key(name, *args, **kwargs)
    now = time.time()
    # Check cache
    if key in _cache:
        result, ts = _cache[key]
        if now - ts < CACHE_TTL:
            return result
    # Not cached or expired; fetch new data
    result = await path_func(*args, **kwargs)
    _cache[key] = (result, now)
    return result


@app.get("/api/health")
async def health() -> Dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/api/blocks/tip/height")
async def get_tip_height() -> Union[int, str]:
    """Return the latest Bitcoin block height."""
    async def fetch():
        return await _fetch_from_upstreams("/blocks/tip/height")
    data = await _get_cached_or_fetch("tip_height", fetch)
    # Attempt to parse integer if possible
    try:
        return int(data)
    except (ValueError, TypeError):
        return data


@app.get("/api/block-height/{height}")
async def get_block_hash(height: str) -> Any:
    """Return the block hash for the given height."""
    async def fetch(h: str):
        return await _fetch_from_upstreams(f"/block-height/{h}")
    data = await _get_cached_or_fetch("block_height", fetch, height)
    return data


@app.get("/api/block/{block_hash}")
async def get_block(block_hash: str) -> Any:
    """Return block details for the given block hash."""
    async def fetch(bh: str):
        return await _fetch_from_upstreams(f"/block/{bh}")
    data = await _get_cached_or_fetch("block", fetch, block_hash)
    return data


@app.get("/api/tx/{txid}")
async def get_transaction(txid: str) -> Any:
    """Return transaction details for the given transaction ID."""
    async def fetch(t: str):
        return await _fetch_from_upstreams(f"/tx/{t}")
    data = await _get_cached_or_fetch("tx", fetch, txid)
    return data


@app.get("/api/address/{address}/utxo")
async def get_utxo(address: str) -> Any:
    """Return the list of unspent outputs for the given address."""
    async def fetch(addr: str):
        return await _fetch_from_upstreams(f"/address/{addr}/utxo")
    data = await _get_cached_or_fetch("utxo", fetch, address)
    return data


@app.get("/api/address/{address}/txs")
async def get_address_txs(address: str) -> Any:
    """Return the transaction list for the given address."""
    async def fetch(addr: str):
        return await _fetch_from_upstreams(f"/address/{addr}/txs")
    data = await _get_cached_or_fetch("address_txs", fetch, address)
    return data


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host=host, port=port, reload=False)
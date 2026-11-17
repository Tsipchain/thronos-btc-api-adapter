"""Input validators for Bitcoin API path parameters and ingest payloads."""

import re
from typing import Any, Optional, Tuple
from fastapi import HTTPException

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_HEIGHT = re.compile(r"^\d{1,8}$")
_ADDRESS = re.compile(r"^(1|3|bc1)[a-zA-HJ-NP-Z0-9]{25,90}$")

MAX_HEIGHT = 10_000_000

INGEST_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MB
INGEST_MAX_ITEMS = 100

ALLOWED_INGEST_KEYS = {
    "tip_height",
    "tip_hash",
    "mempool_info",
    "chain_info",
}

_INGEST_PREFIXED = re.compile(
    r"^(block_height|block|tx|address|utxo|address_txs|address_mempool):(.+)$"
)

_PREFIX_VALIDATORS = {
    "block_height": lambda v: bool(_HEIGHT.match(v)) and int(v) <= MAX_HEIGHT,
    "block": lambda v: bool(_HEX64.match(v)),
    "tx": lambda v: bool(_HEX64.match(v)),
    "address": lambda v: bool(_ADDRESS.match(v)),
    "utxo": lambda v: bool(_ADDRESS.match(v)),
    "address_txs": lambda v: bool(_ADDRESS.match(v)),
    "address_mempool": lambda v: bool(_ADDRESS.match(v)),
}


def validate_block_hash(block_hash: str) -> str:
    if not _HEX64.match(block_hash):
        raise HTTPException(status_code=400, detail="Invalid block hash: must be 64 hex characters")
    return block_hash.lower()


def validate_block_hash_value(block_hash: Any) -> Optional[str]:
    """Non-raising validator for ingest payloads. Returns normalized hash or None."""
    if not isinstance(block_hash, str) or not _HEX64.match(block_hash):
        return None
    return block_hash.lower()


def validate_txid(txid: str) -> str:
    if not _HEX64.match(txid):
        raise HTTPException(status_code=400, detail="Invalid txid: must be 64 hex characters")
    return txid.lower()


def validate_height(height: str) -> str:
    if not _HEIGHT.match(height):
        raise HTTPException(status_code=400, detail="Invalid height: must be a non-negative integer")
    h = int(height)
    if h > MAX_HEIGHT:
        raise HTTPException(status_code=400, detail=f"Height exceeds maximum ({MAX_HEIGHT})")
    return str(h)


def validate_address(address: str) -> str:
    if not _ADDRESS.match(address):
        raise HTTPException(status_code=400, detail="Invalid Bitcoin address format")
    return address


def validate_ingest_key(key: str) -> Optional[str]:
    """Validate an ingest key against allowed namespaces.

    Returns the normalized key if valid, None if rejected.
    """
    if not isinstance(key, str) or len(key) > 256:
        return None

    if key in ALLOWED_INGEST_KEYS:
        return key

    m = _INGEST_PREFIXED.match(key)
    if m:
        prefix, suffix = m.group(1), m.group(2)
        validator = _PREFIX_VALIDATORS.get(prefix)
        if validator and validator(suffix):
            return f"{prefix}:{suffix}"

    return None


def validate_ingest_value(key: str, value: Any) -> Tuple[bool, Any]:
    """Validate and normalize an ingest value based on its key.

    Returns (valid, normalized_value).
    """
    if key == "tip_height":
        try:
            h = int(value)
            if h < 0 or h > MAX_HEIGHT:
                return False, None
            return True, h
        except (ValueError, TypeError):
            return False, None

    if key == "tip_hash":
        if not isinstance(value, str) or not _HEX64.match(value):
            return False, None
        return True, value.lower()

    m = _INGEST_PREFIXED.match(key)
    if m:
        prefix = m.group(1)
        if prefix == "block_height":
            if isinstance(value, str) and _HEX64.match(value):
                return True, value.lower()
            return False, None

    return True, value

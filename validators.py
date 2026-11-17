"""Input validators for Bitcoin API path parameters."""

import re
from fastapi import HTTPException

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_HEIGHT = re.compile(r"^\d{1,8}$")
_ADDRESS = re.compile(r"^(1|3|bc1)[a-zA-HJ-NP-Z0-9]{25,90}$")

MAX_HEIGHT = 10_000_000


def validate_block_hash(block_hash: str) -> str:
    if not _HEX64.match(block_hash):
        raise HTTPException(status_code=400, detail="Invalid block hash: must be 64 hex characters")
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

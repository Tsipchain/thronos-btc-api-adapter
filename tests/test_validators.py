"""Tests for input validators."""

import pytest
from fastapi import HTTPException

from validators import (
    validate_address,
    validate_block_hash,
    validate_block_hash_value,
    validate_height,
    validate_txid,
    validate_ingest_key,
    validate_ingest_value,
)


class TestValidateBlockHash:
    def test_valid_hash(self):
        h = "0000000000000000000233d859be8dab41e05625bfa0bfa9e2b4c8231afb6ed4"
        assert validate_block_hash(h) == h

    def test_uppercase_normalized(self):
        h = "0000000000000000000233D859BE8DAB41E05625BFA0BFA9E2B4C8231AFB6ED4"
        assert validate_block_hash(h) == h.lower()

    def test_too_short(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_block_hash("abcdef")
        assert exc_info.value.status_code == 400

    def test_too_long(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_block_hash("a" * 65)
        assert exc_info.value.status_code == 400

    def test_non_hex_64_chars(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_block_hash("g" * 64)
        assert exc_info.value.status_code == 400

    def test_mixed_invalid_64_chars(self):
        with pytest.raises(HTTPException):
            validate_block_hash("abcdef0123456789" * 3 + "ZZZZZZZZZZZZZZZZ")

    def test_path_traversal(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_block_hash("../../etc/passwd" + "0" * 48)
        assert exc_info.value.status_code == 400

    def test_empty(self):
        with pytest.raises(HTTPException):
            validate_block_hash("")

    def test_slash_injection(self):
        with pytest.raises(HTTPException):
            validate_block_hash("a" * 32 + "/../../" + "a" * 25)


class TestValidateBlockHashValue:
    def test_valid(self):
        h = "a" * 64
        assert validate_block_hash_value(h) == h

    def test_64_non_hex(self):
        assert validate_block_hash_value("z" * 64) is None

    def test_short(self):
        assert validate_block_hash_value("abcdef") is None

    def test_long(self):
        assert validate_block_hash_value("a" * 65) is None

    def test_mixed_invalid(self):
        assert validate_block_hash_value("0123456789abcdef" * 3 + "GHIJKLMNOPQRSTUV") is None

    def test_normalizes_to_lower(self):
        h = "A" * 64
        assert validate_block_hash_value(h) == "a" * 64


class TestValidateTxid:
    def test_valid_txid(self):
        t = "d21633ba23f70118185227be58a63527675641ad37967e2aa461559f577aec43"
        assert validate_txid(t) == t

    def test_invalid_txid(self):
        with pytest.raises(HTTPException):
            validate_txid("not-a-txid")


class TestValidateHeight:
    def test_valid_height(self):
        assert validate_height("870000") == "870000"

    def test_zero(self):
        assert validate_height("0") == "0"

    def test_negative(self):
        with pytest.raises(HTTPException):
            validate_height("-1")

    def test_too_large(self):
        with pytest.raises(HTTPException):
            validate_height("99999999")

    def test_non_numeric(self):
        with pytest.raises(HTTPException):
            validate_height("abc")

    def test_float(self):
        with pytest.raises(HTTPException):
            validate_height("100.5")

    def test_max_boundary(self):
        assert validate_height("10000000") == "10000000"

    def test_over_max(self):
        with pytest.raises(HTTPException):
            validate_height("10000001")


class TestValidateAddress:
    def test_p2pkh(self):
        addr = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
        assert validate_address(addr) == addr

    def test_p2sh(self):
        addr = "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"
        assert validate_address(addr) == addr

    def test_bech32(self):
        addr = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
        assert validate_address(addr) == addr

    def test_invalid_prefix(self):
        with pytest.raises(HTTPException):
            validate_address("4J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy")

    def test_too_short(self):
        with pytest.raises(HTTPException):
            validate_address("1A")

    def test_path_traversal(self):
        with pytest.raises(HTTPException):
            validate_address("../../etc/passwd")

    def test_empty(self):
        with pytest.raises(HTTPException):
            validate_address("")


class TestValidateIngestKey:
    def test_allowed_exact_keys(self):
        for key in ("tip_height", "tip_hash", "mempool_info", "chain_info"):
            assert validate_ingest_key(key) == key

    def test_allowed_prefixed_block_height(self):
        assert validate_ingest_key("block_height:870000") == "block_height:870000"

    def test_allowed_prefixed_block(self):
        h = "a" * 64
        assert validate_ingest_key(f"block:{h}") == f"block:{h}"

    def test_allowed_prefixed_tx(self):
        h = "b" * 64
        assert validate_ingest_key(f"tx:{h}") == f"tx:{h}"

    def test_reject_arbitrary_key(self):
        assert validate_ingest_key("some_random_key") is None

    def test_reject_too_long(self):
        assert validate_ingest_key("a" * 257) is None

    def test_reject_non_string(self):
        assert validate_ingest_key(123) is None

    def test_reject_invalid_prefix_suffix(self):
        assert validate_ingest_key("block:not-a-hash") is None

    def test_reject_block_height_non_numeric(self):
        assert validate_ingest_key("block_height:abc") is None


class TestValidateIngestValue:
    def test_tip_height_valid(self):
        ok, val = validate_ingest_value("tip_height", 870000)
        assert ok is True
        assert val == 870000

    def test_tip_height_negative(self):
        ok, _ = validate_ingest_value("tip_height", -1)
        assert ok is False

    def test_tip_height_string(self):
        ok, val = validate_ingest_value("tip_height", "870000")
        assert ok is True
        assert val == 870000

    def test_tip_hash_valid(self):
        ok, val = validate_ingest_value("tip_hash", "A" * 64)
        assert ok is True
        assert val == "a" * 64

    def test_tip_hash_invalid(self):
        ok, _ = validate_ingest_value("tip_hash", "not-hex")
        assert ok is False

    def test_block_height_value_valid(self):
        ok, val = validate_ingest_value("block_height:870000", "a" * 64)
        assert ok is True
        assert val == "a" * 64

    def test_block_height_value_invalid(self):
        ok, _ = validate_ingest_value("block_height:870000", "not-hex")
        assert ok is False

    def test_passthrough_for_other_keys(self):
        ok, val = validate_ingest_value("chain_info", {"chain": "main"})
        assert ok is True
        assert val == {"chain": "main"}

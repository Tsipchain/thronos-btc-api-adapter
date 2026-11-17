"""Tests for input validators."""

import pytest
from fastapi import HTTPException

from validators import validate_address, validate_block_hash, validate_height, validate_txid


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

    def test_non_hex(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_block_hash("g" * 64)
        assert exc_info.value.status_code == 400

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

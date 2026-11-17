from pathlib import Path
import tempfile

import pytest

from deposit_store import DepositStore, DepositStoreError, MAINNET_GENESIS_HASH


def payload():
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
        "amount_sats": 12345,
        "confirmations": 6,
        "minimum_confirmations": 6,
        "block_hash": "aa" * 32,
        "block_height": 976310,
        "requires_reconciliation": False,
    }


def test_accept_is_durable_and_idempotent():
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "deposits.db")
        store = DepositStore(path)
        first = store.accept(payload())
        assert first.status == "accepted"
        reopened = DepositStore(path)
        second = reopened.accept(payload())
        assert second.status == "duplicate"
        assert second.received_at == first.received_at
        assert reopened.count() == 1


def test_same_key_different_payload_fails_closed():
    with tempfile.TemporaryDirectory() as td:
        store = DepositStore(str(Path(td) / "deposits.db"))
        store.accept(payload())
        changed = payload()
        changed["amount_sats"] = 99999
        with pytest.raises(DepositStoreError, match="idempotency_conflict"):
            store.accept(changed)
        assert store.count() == 1

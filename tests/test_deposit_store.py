from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
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


def test_simultaneous_duplicate_delivery_is_deterministic():
    with tempfile.TemporaryDirectory() as td:
        store = DepositStore(str(Path(td) / "deposits.db"))
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _index: store.accept(payload()), range(32)))
        assert [result.status for result in results].count("accepted") == 1
        assert [result.status for result in results].count("duplicate") == 31
        assert store.count() == 1


def test_simultaneous_conflicting_delivery_fails_closed():
    with tempfile.TemporaryDirectory() as td:
        store = DepositStore(str(Path(td) / "deposits.db"))
        changed = payload()
        changed["amount_sats"] = 99999
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(store.accept, item) for item in (payload(), changed)]
        accepted = 0
        conflicts = 0
        for future in futures:
            try:
                accepted += int(future.result().status == "accepted")
            except DepositStoreError as exc:
                assert str(exc) == "deposit_idempotency_conflict"
                conflicts += 1
        assert (accepted, conflicts) == (1, 1)
        assert store.count() == 1


def test_database_network_identity_is_durable():
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "deposits.db")
        DepositStore(path)
        DepositStore(path)
        with pytest.raises(DepositStoreError, match="network_identity_mismatch"):
            DepositStore(path, network="test", genesis_hash="22" * 32)


def test_pr4_database_is_safely_bound_during_additive_migration():
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "deposits.db")
        store = DepositStore(path)
        store.accept(payload())
        with sqlite3.connect(path) as con:
            con.execute("DROP TABLE btc_deposit_store_identity")

        reopened = DepositStore(path)
        assert reopened.accept(payload()).status == "duplicate"
        with sqlite3.connect(path) as con:
            identity = con.execute(
                """SELECT network, genesis_hash
                   FROM btc_deposit_store_identity WHERE singleton=1"""
            ).fetchone()
        assert identity == ("main", MAINNET_GENESIS_HASH)


def test_store_rejects_payload_from_another_network_identity():
    with tempfile.TemporaryDirectory() as td:
        store = DepositStore(str(Path(td) / "deposits.db"))
        changed = payload()
        changed["genesis_hash"] = "22" * 32
        with pytest.raises(DepositStoreError, match="network_identity_mismatch"):
            store.accept(changed)
        assert store.count() == 0

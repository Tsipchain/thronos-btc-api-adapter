from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile

import pytest

from bitcoin_verifier import (
    CanonicalDepositProof,
    ReorgDetected,
    VerificationConflict,
    VerificationUnavailable,
)
from deposit_store import DepositStore, MAINNET_GENESIS_HASH
from settlement_handoff import (
    CANONICALLY_VERIFIED,
    COMPENSATION_PENDING,
    CREDITED,
    HELD,
    QUARANTINE,
    RECEIPT_STORED,
    RECONCILIATION_REQUIRED,
    SETTLEMENT_PENDING,
    VERIFICATION_PENDING,
    SettlementHandoffError,
    SettlementHandoffStore,
)


TXID = "11" * 32
BLOCK_HASH = "aa" * 32
TIP_HASH = "bb" * 32
ADDRESS = "bc1qsettlementhandoff0000000000000000000000"
OWNER_REF = "bridge-intent-42"
KEY = f"main:{TXID}:0"


def receipt(**changes):
    result = {
        "network": "main",
        "genesis_hash": MAINNET_GENESIS_HASH,
        "event_id": KEY,
        "idempotency_key": KEY,
        "txid": TXID,
        "vout": 0,
        "address": ADDRESS,
        "owner_ref": OWNER_REF,
        "amount_sats": 12_345,
        "confirmations": 6,
        "minimum_confirmations": 6,
        "block_hash": BLOCK_HASH,
        "block_height": 100,
        "requires_reconciliation": False,
    }
    result.update(changes)
    return result


def proof(**changes):
    result = {
        "proof_ref": "btcp_" + "44" * 32,
        "event_id": KEY,
        "network": "main",
        "txid": TXID,
        "vout": 0,
        "address": ADDRESS,
        "owner_ref": OWNER_REF,
        "amount_sats": 12_345,
        "script_pubkey_hex": "0014" + "12" * 20,
        "block_hash": BLOCK_HASH,
        "block_height": 100,
        "confirmations": 6,
        "tip_height": 105,
        "tip_hash": TIP_HASH,
        "registry_version": "registry-v7",
        "registry_digest": "55" * 32,
        "verifier_source": "primary",
        "verified_at": 1_800_000_000,
    }
    result.update(changes)
    return CanonicalDepositProof(**result)


class ResultVerifier:
    def __init__(self, result):
        self.result = result

    def verify(self, _candidate):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def stores():
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "deposits.db")
        deposits = DepositStore(path)
        deposits.accept(receipt())
        handoff = SettlementHandoffStore(path, now_fn=lambda: 1_800_000_001)
        yield path, deposits, handoff


def test_receipt_acceptance_only_creates_receipt_stored_state(stores):
    _path, _deposits, handoff = stores
    state = handoff.state(KEY)
    assert state["state"] == RECEIPT_STORED
    assert state["proof_ref"] is None
    assert state["settlement_command_id"] is None
    assert [item["event_type"] for item in handoff.audit(KEY)] == ["receipt_stored"]


def test_valid_proof_creates_one_durable_non_delivering_command(stores):
    _path, _deposits, handoff = stores
    verified = handoff.verify_receipt(KEY, ResultVerifier(proof()))
    assert verified["state"] == CANONICALLY_VERIFIED

    command = handoff.prepare_settlement(KEY, ResultVerifier(proof()))
    replay = handoff.prepare_settlement(KEY, ResultVerifier(proof()))

    assert command == replay
    assert command.command_id == f"btc-settle:{KEY}"
    assert command.idempotency_key == KEY
    assert command.owner_ref == OWNER_REF
    assert command.amount_sats == 12_345
    assert command.state == "pending"
    assert handoff.state(KEY)["state"] == SETTLEMENT_PENDING
    assert handoff.pending_commands() == [command]
    with pytest.raises(SettlementHandoffError, match="credit_acknowledgement_disabled"):
        handoff.acknowledge_credit(
            command.command_id, accounting_receipt_ref="ledger-receipt-1"
        )


def test_unavailable_proof_is_retryable_then_verifies(stores):
    _path, _deposits, handoff = stores
    pending = handoff.verify_receipt(
        KEY, ResultVerifier(VerificationUnavailable("archival_transaction_proof_required"))
    )
    assert pending["state"] == VERIFICATION_PENDING
    assert pending["last_error"] == "archival_transaction_proof_required"

    verified = handoff.verify_receipt(KEY, ResultVerifier(proof()))
    assert verified["state"] == CANONICALLY_VERIFIED
    assert verified["last_error"] is None


def test_conflicting_proof_is_quarantined_without_command(stores):
    _path, _deposits, handoff = stores
    state = handoff.verify_receipt(
        KEY, ResultVerifier(VerificationConflict("candidate_amount_mismatch"))
    )
    assert state["state"] == QUARANTINE
    assert handoff.pending_commands() == []
    with pytest.raises(SettlementHandoffError, match="not_canonically_verified"):
        handoff.prepare_settlement(KEY, ResultVerifier(proof()))


def test_reorg_before_handoff_requires_reconciliation(stores):
    _path, _deposits, handoff = stores
    state = handoff.verify_receipt(
        KEY, ResultVerifier(ReorgDetected("reorg_detected_before_handoff"))
    )
    assert state["state"] == RECONCILIATION_REQUIRED
    assert handoff.pending_commands() == []


def test_reorg_on_immediate_pre_command_refresh_blocks_handoff(stores):
    _path, _deposits, handoff = stores
    handoff.record_canonical_proof(KEY, proof())

    with pytest.raises(SettlementHandoffError, match="not_canonically_verified"):
        handoff.prepare_settlement(
            KEY, ResultVerifier(ReorgDetected("reorg_detected_before_handoff"))
        )

    assert handoff.state(KEY)["state"] == RECONCILIATION_REQUIRED
    assert handoff.pending_commands() == []


def test_proof_must_match_durable_receipt(stores):
    _path, _deposits, handoff = stores
    with pytest.raises(SettlementHandoffError, match="proof_receipt_conflict"):
        handoff.record_canonical_proof(KEY, proof(amount_sats=12_346))
    assert handoff.state(KEY)["state"] == RECEIPT_STORED


def test_concurrent_command_preparation_is_idempotent(stores):
    path, _deposits, handoff = stores
    handoff.record_canonical_proof(KEY, proof())

    def prepare(_index):
        return SettlementHandoffStore(path).prepare_settlement(
            KEY, ResultVerifier(proof())
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        commands = list(pool.map(prepare, range(32)))

    assert {command.command_id for command in commands} == {f"btc-settle:{KEY}"}
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM btc_settlement_commands").fetchone()[0] == 1


def test_command_transaction_rolls_back_and_retry_recovers(stores):
    path, _deposits, handoff = stores
    handoff.record_canonical_proof(KEY, proof())
    with sqlite3.connect(path) as con:
        con.execute(
            """CREATE TRIGGER fail_command_insert BEFORE INSERT ON btc_settlement_commands
               BEGIN SELECT RAISE(ABORT, 'synthetic crash'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic crash"):
        handoff.prepare_settlement(KEY, ResultVerifier(proof()))
    assert handoff.state(KEY)["state"] == CANONICALLY_VERIFIED
    assert handoff.pending_commands() == []

    with sqlite3.connect(path) as con:
        con.execute("DROP TRIGGER fail_command_insert")
    recovered = handoff.prepare_settlement(KEY, ResultVerifier(proof()))
    assert recovered.command_id == f"btc-settle:{KEY}"
    assert handoff.state(KEY)["state"] == SETTLEMENT_PENDING


def test_database_restart_preserves_state_proof_command_and_audit(stores):
    path, _deposits, handoff = stores
    handoff.record_canonical_proof(KEY, proof())
    command = handoff.prepare_settlement(KEY, ResultVerifier(proof()))

    reopened = SettlementHandoffStore(path)
    assert reopened.state(KEY)["state"] == SETTLEMENT_PENDING
    assert reopened.pending_commands() == [command]
    assert [row["event_type"] for row in reopened.audit(KEY)] == [
        "receipt_stored",
        "canonical_proof",
        "canonical_proof_refresh",
        "settlement_command",
    ]


def test_pr5_receipt_database_gets_additive_state_and_audit_migration():
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "deposits.db")
        deposits = DepositStore(path)
        deposits.accept(receipt())
        with sqlite3.connect(path) as con:
            con.execute("PRAGMA foreign_keys=OFF")
            con.execute("DROP TABLE btc_settlement_commands")
            con.execute("DROP TABLE btc_settlement_state")
            con.execute("DROP TABLE btc_settlement_audit")

        DepositStore(path)
        migrated = SettlementHandoffStore(path)
        assert migrated.state(KEY)["state"] == RECEIPT_STORED
        assert [row["event_type"] for row in migrated.audit(KEY)] == [
            "receipt_stored"
        ]


def test_duplicate_receipt_does_not_reset_or_duplicate_handoff(stores):
    _path, deposits, handoff = stores
    handoff.record_canonical_proof(KEY, proof())
    command = handoff.prepare_settlement(KEY, ResultVerifier(proof()))
    assert deposits.accept(receipt()).status == "duplicate"
    assert handoff.state(KEY)["state"] == SETTLEMENT_PENDING
    assert handoff.pending_commands() == [command]


def test_credited_fixture_reorg_becomes_compensation_pending_then_held(stores):
    path, _deposits, handoff = stores
    handoff.record_canonical_proof(KEY, proof())
    command = handoff.prepare_settlement(KEY, ResultVerifier(proof()))
    fixture = SettlementHandoffStore(
        path, credit_ack_enabled=True, now_fn=lambda: 1_800_000_002
    )
    credited = fixture.acknowledge_credit(
        command.command_id, accounting_receipt_ref="synthetic-ledger-receipt-1"
    )
    assert credited["state"] == CREDITED

    reorged = fixture.record_reorg(KEY, "canonical_block_removed")
    assert reorged["state"] == COMPENSATION_PENDING
    assert fixture.pending_commands() == []
    held = fixture.hold_compensation(KEY, hold_ref="synthetic-hold-1")
    assert held["state"] == HELD


def test_accounting_acknowledgement_is_idempotent_and_conflicts_fail_closed(stores):
    path, _deposits, handoff = stores
    handoff.record_canonical_proof(KEY, proof())
    command = handoff.prepare_settlement(KEY, ResultVerifier(proof()))
    fixture = SettlementHandoffStore(path, credit_ack_enabled=True)
    first = fixture.acknowledge_credit(
        command.command_id, accounting_receipt_ref="synthetic-ledger-receipt-1"
    )
    second = fixture.acknowledge_credit(
        command.command_id, accounting_receipt_ref="synthetic-ledger-receipt-1"
    )
    assert first["state"] == second["state"] == CREDITED
    with pytest.raises(SettlementHandoffError, match="accounting_receipt_conflict"):
        fixture.acknowledge_credit(
            command.command_id, accounting_receipt_ref="different-receipt"
        )

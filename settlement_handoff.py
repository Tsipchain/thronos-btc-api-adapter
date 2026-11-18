"""Durable, non-delivering Bitcoin settlement handoff state machine.

Receipt acknowledgement and canonical verification never credit balances here.
The furthest normal production transition is ``settlement_pending`` with a
durable command for an independently authorized accounting consumer.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import secrets
import sqlite3
import time
from typing import Any, Mapping

from bitcoin_verifier import (
    CanonicalDepositProof,
    DepositCandidate,
    IndependentBitcoinVerifier,
    ReorgDetected,
    VerificationConflict,
    VerificationUnavailable,
)


RECEIPT_STORED = "receipt_stored"
VERIFICATION_PENDING = "verification_pending"
CANONICALLY_VERIFIED = "canonically_verified"
SETTLEMENT_PENDING = "settlement_pending"
CREDITED = "credited"
QUARANTINE = "quarantine"
RECONCILIATION_REQUIRED = "reconciliation_required"
COMPENSATION_PENDING = "compensation_pending"
HELD = "held"

VALID_STATES = frozenset(
    {
        RECEIPT_STORED,
        VERIFICATION_PENDING,
        CANONICALLY_VERIFIED,
        SETTLEMENT_PENDING,
        CREDITED,
        QUARANTINE,
        RECONCILIATION_REQUIRED,
        COMPENSATION_PENDING,
        HELD,
    }
)


class SettlementHandoffError(RuntimeError):
    pass


@dataclass(frozen=True)
class SettlementCommand:
    command_id: str
    idempotency_key: str
    event_id: str
    owner_ref: str
    amount_sats: int
    proof_ref: str
    payload_sha256: str
    state: str
    created_at: int


def init_settlement_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS btc_settlement_state (
            idempotency_key TEXT PRIMARY KEY,
            state TEXT NOT NULL CHECK(state IN (
                'receipt_stored','verification_pending','canonically_verified',
                'settlement_pending','credited','quarantine',
                'reconciliation_required','compensation_pending','held'
            )),
            proof_ref TEXT,
            proof_json TEXT,
            settlement_command_id TEXT UNIQUE,
            accounting_receipt_ref TEXT,
            last_error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(idempotency_key)
                REFERENCES btc_deposit_events(idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_btc_settlement_state
            ON btc_settlement_state(state, updated_at);
        CREATE TABLE IF NOT EXISTS btc_settlement_commands (
            command_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            event_id TEXT NOT NULL,
            owner_ref TEXT NOT NULL,
            amount_sats INTEGER NOT NULL,
            proof_ref TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','acknowledged','held')),
            created_at INTEGER NOT NULL,
            acknowledged_at INTEGER,
            FOREIGN KEY(idempotency_key)
                REFERENCES btc_settlement_state(idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_btc_settlement_commands_state
            ON btc_settlement_commands(state, created_at);
        CREATE TABLE IF NOT EXISTS btc_settlement_audit (
            audit_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_ref TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT NOT NULL,
            details_json TEXT NOT NULL,
            recorded_at INTEGER NOT NULL,
            UNIQUE(idempotency_key, event_type, event_ref)
        );
        """
    )
    now = int(time.time())
    con.execute(
        """INSERT OR IGNORE INTO btc_settlement_state
           (idempotency_key, state, created_at, updated_at)
           SELECT idempotency_key, 'receipt_stored', received_at, ?
             FROM btc_deposit_events""",
        (now,),
    )
    con.execute(
        """INSERT OR IGNORE INTO btc_settlement_audit
           (audit_id, idempotency_key, event_type, event_ref, from_state,
            to_state, details_json, recorded_at)
           SELECT 'bsa_receipt:' || idempotency_key, idempotency_key,
                  'receipt_stored', idempotency_key, NULL,
                  'receipt_stored', '{}', received_at
             FROM btc_deposit_events"""
    )


def register_receipt_in_tx(
    con: sqlite3.Connection, idempotency_key: str, received_at: int
) -> None:
    inserted = con.execute(
        """INSERT OR IGNORE INTO btc_settlement_state
           (idempotency_key, state, created_at, updated_at)
           VALUES (?, 'receipt_stored', ?, ?)""",
        (idempotency_key, received_at, received_at),
    )
    if inserted.rowcount == 1:
        _audit_in_tx(
            con,
            idempotency_key,
            "receipt_stored",
            idempotency_key,
            None,
            RECEIPT_STORED,
            {},
            received_at,
        )


class SettlementHandoffStore:
    """Persist proof and commands without delivering or applying a credit."""

    def __init__(
        self,
        db_path: str,
        *,
        credit_ack_enabled: bool = False,
        now_fn=time.time,
    ) -> None:
        self.db_path = db_path
        self.credit_ack_enabled = credit_ack_enabled
        self.now_fn = now_fn
        with self._connect() as con:
            init_settlement_schema(con)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        return con

    def state(self, idempotency_key: str) -> dict[str, Any]:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM btc_settlement_state WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        if not row:
            raise SettlementHandoffError("settlement_receipt_not_found")
        return dict(row)

    def verify_receipt(
        self,
        idempotency_key: str,
        verifier: IndependentBitcoinVerifier,
    ) -> dict[str, Any]:
        candidate = self._candidate(idempotency_key)
        try:
            proof = verifier.verify(candidate)
        except VerificationUnavailable as exc:
            return self.mark_verification_pending(idempotency_key, exc.code)
        except ReorgDetected as exc:
            return self.record_reorg(idempotency_key, exc.code)
        except VerificationConflict as exc:
            return self.quarantine(idempotency_key, exc.code)
        return self.record_canonical_proof(idempotency_key, proof)

    def record_canonical_proof(
        self,
        idempotency_key: str,
        proof: CanonicalDepositProof,
    ) -> dict[str, Any]:
        proof_record = proof.to_record()
        if proof.event_id != idempotency_key:
            raise SettlementHandoffError("settlement_proof_identity_mismatch")
        encoded = _canonical_json(proof_record)
        now = int(self.now_fn())
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            receipt = _receipt_in_tx(con, idempotency_key)
            _proof_matches_receipt(receipt, proof_record)
            row = _state_in_tx(con, idempotency_key)
            if row["state"] in (SETTLEMENT_PENDING, CREDITED):
                if row["proof_ref"] != proof.proof_ref or row["proof_json"] != encoded:
                    raise SettlementHandoffError("settlement_proof_conflict")
                return dict(row)
            if row["state"] not in (
                RECEIPT_STORED,
                VERIFICATION_PENDING,
                CANONICALLY_VERIFIED,
            ):
                raise SettlementHandoffError("settlement_state_not_verifiable")
            event_type = (
                "canonical_proof_refresh"
                if row["state"] == CANONICALLY_VERIFIED
                else "canonical_proof"
            )
            con.execute(
                """UPDATE btc_settlement_state
                   SET state=?, proof_ref=?, proof_json=?, last_error=NULL, updated_at=?
                   WHERE idempotency_key=?""",
                (CANONICALLY_VERIFIED, proof.proof_ref, encoded, now, idempotency_key),
            )
            _audit_in_tx(
                con,
                idempotency_key,
                event_type,
                proof.proof_ref,
                row["state"],
                CANONICALLY_VERIFIED,
                {"proof_ref": proof.proof_ref},
                now,
            )
            return dict(_state_in_tx(con, idempotency_key))

    def mark_verification_pending(self, idempotency_key: str, reason: str) -> dict[str, Any]:
        return self._failure_transition(
            idempotency_key,
            reason,
            target=VERIFICATION_PENDING,
            allowed=(RECEIPT_STORED, VERIFICATION_PENDING, CANONICALLY_VERIFIED),
            event_type="verification_unavailable",
        )

    def quarantine(self, idempotency_key: str, reason: str) -> dict[str, Any]:
        return self._failure_transition(
            idempotency_key,
            reason,
            target=QUARANTINE,
            allowed=(RECEIPT_STORED, VERIFICATION_PENDING, CANONICALLY_VERIFIED),
            event_type="verification_conflict",
        )

    def record_reorg(self, idempotency_key: str, reason: str) -> dict[str, Any]:
        now = int(self.now_fn())
        event_ref = _reason_ref(reason)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = _state_in_tx(con, idempotency_key)
            if row["state"] in (CREDITED, COMPENSATION_PENDING, HELD):
                target = COMPENSATION_PENDING if row["state"] == CREDITED else row["state"]
            else:
                target = RECONCILIATION_REQUIRED
            con.execute(
                """UPDATE btc_settlement_state
                   SET state=?, last_error=?, updated_at=? WHERE idempotency_key=?""",
                (target, reason, now, idempotency_key),
            )
            if row["settlement_command_id"]:
                con.execute(
                    """UPDATE btc_settlement_commands SET state='held'
                       WHERE command_id=? AND state IN ('pending','acknowledged')""",
                    (row["settlement_command_id"],),
                )
            _audit_in_tx(
                con,
                idempotency_key,
                "reorg",
                event_ref,
                row["state"],
                target,
                {"reason": reason},
                now,
            )
            return dict(_state_in_tx(con, idempotency_key))

    def prepare_settlement(
        self,
        idempotency_key: str,
        verifier: IndependentBitcoinVerifier,
    ) -> SettlementCommand:
        # A durable command can be replayed without new chain evidence. Before
        # creating it for the first time, however, refresh canonical proof so
        # reorg and confirmation state are checked immediately before handoff.
        existing = self.state(idempotency_key)
        if existing["state"] == SETTLEMENT_PENDING:
            with self._connect() as con:
                return _command_from_row(
                    _command_in_tx(con, str(existing["settlement_command_id"]))
                )
        if existing["state"] not in (
            RECEIPT_STORED,
            VERIFICATION_PENDING,
            CANONICALLY_VERIFIED,
        ):
            raise SettlementHandoffError("settlement_not_canonically_verified")
        refreshed = self.verify_receipt(idempotency_key, verifier)
        if refreshed["state"] == SETTLEMENT_PENDING:
            with self._connect() as con:
                return _command_from_row(
                    _command_in_tx(con, str(refreshed["settlement_command_id"]))
                )
        if refreshed["state"] != CANONICALLY_VERIFIED:
            raise SettlementHandoffError("settlement_not_canonically_verified")
        now = int(self.now_fn())
        command_id = f"btc-settle:{idempotency_key}"
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            receipt = _receipt_in_tx(con, idempotency_key)
            row = _state_in_tx(con, idempotency_key)
            if row["state"] == SETTLEMENT_PENDING:
                return _command_from_row(_command_in_tx(con, command_id))
            if row["state"] != CANONICALLY_VERIFIED or not row["proof_ref"]:
                raise SettlementHandoffError("settlement_not_canonically_verified")
            payload = {
                "command_id": command_id,
                "idempotency_key": idempotency_key,
                "event_id": receipt["event_id"],
                "owner_ref": receipt["owner_ref"],
                "amount_sats": int(receipt["amount_sats"]),
                "proof_ref": row["proof_ref"],
            }
            encoded = _canonical_json(payload)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            con.execute(
                """INSERT INTO btc_settlement_commands
                   (command_id, idempotency_key, event_id, owner_ref, amount_sats,
                    proof_ref, payload_sha256, payload_json, state, created_at)
                   VALUES (?,?,?,?,?,?,?,?,'pending',?)""",
                (
                    command_id,
                    idempotency_key,
                    receipt["event_id"],
                    receipt["owner_ref"],
                    int(receipt["amount_sats"]),
                    row["proof_ref"],
                    digest,
                    encoded,
                    now,
                ),
            )
            con.execute(
                """UPDATE btc_settlement_state
                   SET state=?, settlement_command_id=?, updated_at=?
                   WHERE idempotency_key=?""",
                (SETTLEMENT_PENDING, command_id, now, idempotency_key),
            )
            _audit_in_tx(
                con,
                idempotency_key,
                "settlement_command",
                command_id,
                row["state"],
                SETTLEMENT_PENDING,
                {"command_id": command_id, "payload_sha256": digest},
                now,
            )
            return _command_from_row(_command_in_tx(con, command_id))

    def pending_commands(self, *, limit: int = 100) -> list[SettlementCommand]:
        if limit < 1 or limit > 1000:
            raise SettlementHandoffError("settlement_command_limit_invalid")
        with self._connect() as con:
            rows = con.execute(
                """SELECT * FROM btc_settlement_commands
                   WHERE state='pending' ORDER BY created_at, command_id LIMIT ?""",
                (limit,),
            ).fetchall()
        return [_command_from_row(row) for row in rows]

    def acknowledge_credit(
        self,
        command_id: str,
        *,
        accounting_receipt_ref: str,
    ) -> dict[str, Any]:
        """Record an external atomic-ledger receipt; never apply a credit here."""
        if not self.credit_ack_enabled:
            raise SettlementHandoffError("credit_acknowledgement_disabled")
        receipt_ref = str(accounting_receipt_ref or "").strip()
        if not receipt_ref or len(receipt_ref) > 200:
            raise SettlementHandoffError("accounting_receipt_ref_invalid")
        now = int(self.now_fn())
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            command = _command_in_tx(con, command_id)
            state = _state_in_tx(con, command["idempotency_key"])
            if state["state"] == CREDITED:
                if state["accounting_receipt_ref"] != receipt_ref:
                    raise SettlementHandoffError("accounting_receipt_conflict")
                return dict(state)
            if state["state"] != SETTLEMENT_PENDING or command["state"] != "pending":
                raise SettlementHandoffError("settlement_command_not_acknowledgeable")
            con.execute(
                """UPDATE btc_settlement_commands
                   SET state='acknowledged', acknowledged_at=? WHERE command_id=?""",
                (now, command_id),
            )
            con.execute(
                """UPDATE btc_settlement_state
                   SET state=?, accounting_receipt_ref=?, updated_at=?
                   WHERE idempotency_key=?""",
                (CREDITED, receipt_ref, now, command["idempotency_key"]),
            )
            _audit_in_tx(
                con,
                command["idempotency_key"],
                "accounting_ack",
                receipt_ref,
                state["state"],
                CREDITED,
                {"command_id": command_id, "accounting_receipt_ref": receipt_ref},
                now,
            )
            return dict(_state_in_tx(con, command["idempotency_key"]))

    def hold_compensation(self, idempotency_key: str, *, hold_ref: str) -> dict[str, Any]:
        hold_ref = str(hold_ref or "").strip()
        if not hold_ref or len(hold_ref) > 200:
            raise SettlementHandoffError("compensation_hold_ref_invalid")
        now = int(self.now_fn())
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = _state_in_tx(con, idempotency_key)
            if row["state"] == HELD:
                return dict(row)
            if row["state"] != COMPENSATION_PENDING:
                raise SettlementHandoffError("compensation_not_pending")
            con.execute(
                """UPDATE btc_settlement_state SET state=?, updated_at=?
                   WHERE idempotency_key=?""",
                (HELD, now, idempotency_key),
            )
            _audit_in_tx(
                con,
                idempotency_key,
                "compensation_hold",
                hold_ref,
                row["state"],
                HELD,
                {"hold_ref": hold_ref},
                now,
            )
            return dict(_state_in_tx(con, idempotency_key))

    def audit(self, idempotency_key: str) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """SELECT * FROM btc_settlement_audit
                   WHERE idempotency_key=? ORDER BY rowid""",
                (idempotency_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _candidate(self, idempotency_key: str) -> DepositCandidate:
        with self._connect() as con:
            row = _receipt_in_tx(con, idempotency_key)
        return DepositCandidate.from_mapping(dict(row))

    def _failure_transition(
        self,
        idempotency_key: str,
        reason: str,
        *,
        target: str,
        allowed: tuple[str, ...],
        event_type: str,
    ) -> dict[str, Any]:
        reason = str(reason or "").strip()
        if not reason or len(reason) > 200:
            raise SettlementHandoffError("settlement_failure_reason_invalid")
        now = int(self.now_fn())
        event_ref = _reason_ref(reason)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = _state_in_tx(con, idempotency_key)
            if row["state"] == target and row["last_error"] == reason:
                return dict(row)
            if row["state"] not in allowed:
                raise SettlementHandoffError("settlement_failure_transition_invalid")
            con.execute(
                """UPDATE btc_settlement_state
                   SET state=?, last_error=?, updated_at=? WHERE idempotency_key=?""",
                (target, reason, now, idempotency_key),
            )
            _audit_in_tx(
                con,
                idempotency_key,
                event_type,
                event_ref,
                row["state"],
                target,
                {"reason": reason},
                now,
            )
            return dict(_state_in_tx(con, idempotency_key))


def _receipt_in_tx(con: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT * FROM btc_deposit_events WHERE idempotency_key=?", (key,)
    ).fetchone()
    if not row:
        raise SettlementHandoffError("settlement_receipt_not_found")
    return row


def _state_in_tx(con: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT * FROM btc_settlement_state WHERE idempotency_key=?", (key,)
    ).fetchone()
    if not row:
        raise SettlementHandoffError("settlement_state_not_found")
    return row


def _command_in_tx(con: sqlite3.Connection, command_id: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT * FROM btc_settlement_commands WHERE command_id=?", (command_id,)
    ).fetchone()
    if not row:
        raise SettlementHandoffError("settlement_command_not_found")
    return row


def _proof_matches_receipt(receipt: sqlite3.Row, proof: Mapping[str, Any]) -> None:
    expected = {
        "event_id": str(receipt["event_id"]),
        "network": str(receipt["network"]),
        "txid": str(receipt["txid"]),
        "vout": int(receipt["vout"]),
        "address": str(receipt["address"]),
        "owner_ref": str(receipt["owner_ref"]),
        "amount_sats": int(receipt["amount_sats"]),
        "block_hash": str(receipt["block_hash"]),
        "block_height": int(receipt["block_height"]),
    }
    if any(proof.get(field) != value for field, value in expected.items()):
        raise SettlementHandoffError("settlement_proof_receipt_conflict")


def _command_from_row(row: sqlite3.Row) -> SettlementCommand:
    return SettlementCommand(
        command_id=str(row["command_id"]),
        idempotency_key=str(row["idempotency_key"]),
        event_id=str(row["event_id"]),
        owner_ref=str(row["owner_ref"]),
        amount_sats=int(row["amount_sats"]),
        proof_ref=str(row["proof_ref"]),
        payload_sha256=str(row["payload_sha256"]),
        state=str(row["state"]),
        created_at=int(row["created_at"]),
    )


def _audit_in_tx(
    con: sqlite3.Connection,
    key: str,
    event_type: str,
    event_ref: str,
    from_state: str | None,
    to_state: str,
    details: Mapping[str, Any],
    now: int,
) -> None:
    con.execute(
        """INSERT OR IGNORE INTO btc_settlement_audit
           (audit_id, idempotency_key, event_type, event_ref, from_state,
            to_state, details_json, recorded_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            "bsa_" + secrets.token_hex(12),
            key,
            event_type,
            event_ref,
            from_state,
            to_state,
            _canonical_json(details),
            now,
        ),
    )


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def _reason_ref(reason: str) -> str:
    return hashlib.sha256(reason.encode()).hexdigest()

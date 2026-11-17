"""Durable idempotent storage for authenticated Bitcoin deposit observations.

This module records accepted confirmed-deposit notifications only. It never
credits balances, signs Bitcoin transactions, or performs settlement.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping

MAINNET_GENESIS_HASH = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"


class DepositStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class DepositAcceptResult:
    status: str
    idempotency_key: str
    received_at: int


class DepositStore:
    def __init__(
        self,
        db_path: str,
        *,
        network: str = "main",
        genesis_hash: str = MAINNET_GENESIS_HASH,
    ) -> None:
        path = Path(db_path)
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path)
        self.network = str(network)
        self.genesis_hash = str(genesis_hash).lower()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        return con

    def _init_db(self) -> None:
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS btc_deposit_events (
                    idempotency_key TEXT PRIMARY KEY,
                    network TEXT NOT NULL,
                    genesis_hash TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    txid TEXT NOT NULL,
                    vout INTEGER NOT NULL,
                    address TEXT NOT NULL,
                    owner_ref TEXT NOT NULL,
                    amount_sats INTEGER NOT NULL,
                    confirmations INTEGER NOT NULL,
                    minimum_confirmations INTEGER NOT NULL,
                    block_hash TEXT NOT NULL,
                    block_height INTEGER NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    received_at INTEGER NOT NULL,
                    UNIQUE(network, txid, vout)
                );
                CREATE INDEX IF NOT EXISTS idx_btc_deposit_received
                    ON btc_deposit_events(received_at);
                CREATE TABLE IF NOT EXISTS btc_deposit_store_identity (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    network TEXT NOT NULL,
                    genesis_hash TEXT NOT NULL,
                    bound_at INTEGER NOT NULL
                );
                """
            )
            con.execute("BEGIN IMMEDIATE")
            identity = con.execute(
                """SELECT network, genesis_hash FROM btc_deposit_store_identity
                   WHERE singleton=1"""
            ).fetchone()
            if identity is not None:
                if (
                    str(identity["network"]) != self.network
                    or str(identity["genesis_hash"]).lower() != self.genesis_hash
                ):
                    raise DepositStoreError("deposit_store_network_identity_mismatch")
            else:
                incompatible = con.execute(
                    """SELECT 1 FROM btc_deposit_events
                       WHERE network<>? OR lower(genesis_hash)<>?
                       LIMIT 1""",
                    (self.network, self.genesis_hash),
                ).fetchone()
                if incompatible is not None:
                    raise DepositStoreError("deposit_store_network_identity_mismatch")
                con.execute(
                    """INSERT INTO btc_deposit_store_identity
                       (singleton, network, genesis_hash, bound_at)
                       VALUES(1,?,?,?)""",
                    (self.network, self.genesis_hash, int(time.time())),
                )

    @staticmethod
    def canonical_payload(payload: Mapping[str, Any]) -> tuple[str, str]:
        encoded = json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return encoded, digest

    def accept(self, payload: Mapping[str, Any]) -> DepositAcceptResult:
        if (
            str(payload["network"]) != self.network
            or str(payload["genesis_hash"]).lower() != self.genesis_hash
        ):
            raise DepositStoreError("deposit_store_network_identity_mismatch")
        key = str(payload["idempotency_key"])
        encoded, digest = self.canonical_payload(payload)
        now = int(time.time())
        with self._connect() as con:
            # Serialize the read/insert decision across processes and replicas
            # sharing this SQLite file. This makes a lost-response retry and
            # simultaneous delivery take the same deterministic path.
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                """SELECT idempotency_key, payload_sha256, received_at
                   FROM btc_deposit_events
                   WHERE idempotency_key=?
                      OR (network=? AND txid=? AND vout=?)
                   LIMIT 1""",
                (
                    key,
                    str(payload["network"]),
                    str(payload["txid"]),
                    int(payload["vout"]),
                ),
            ).fetchone()
            if row:
                if (
                    str(row["idempotency_key"]) != key
                    or str(row["payload_sha256"]) != digest
                ):
                    raise DepositStoreError("deposit_idempotency_conflict")
                return DepositAcceptResult(
                    status="duplicate",
                    idempotency_key=key,
                    received_at=int(row["received_at"]),
                )

            try:
                con.execute(
                    """INSERT INTO btc_deposit_events
                       (idempotency_key, network, genesis_hash, event_id, txid, vout,
                        address, owner_ref, amount_sats, confirmations,
                        minimum_confirmations, block_hash, block_height,
                        payload_sha256, payload_json, received_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        key,
                        str(payload["network"]),
                        str(payload["genesis_hash"]),
                        str(payload["event_id"]),
                        str(payload["txid"]),
                        int(payload["vout"]),
                        str(payload["address"]),
                        str(payload["owner_ref"]),
                        int(payload["amount_sats"]),
                        int(payload["confirmations"]),
                        int(payload["minimum_confirmations"]),
                        str(payload["block_hash"]),
                        int(payload["block_height"]),
                        digest,
                        encoded,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # Covers rolling upgrades or any writer not using the same
                # transaction protocol. Never turn a uniqueness race into an
                # ambiguous 500 response.
                row = con.execute(
                    """SELECT idempotency_key, payload_sha256, received_at
                       FROM btc_deposit_events
                       WHERE idempotency_key=?
                          OR (network=? AND txid=? AND vout=?)
                       LIMIT 1""",
                    (
                        key,
                        str(payload["network"]),
                        str(payload["txid"]),
                        int(payload["vout"]),
                    ),
                ).fetchone()
                if (
                    row is not None
                    and str(row["idempotency_key"]) == key
                    and str(row["payload_sha256"]) == digest
                ):
                    return DepositAcceptResult(
                        status="duplicate",
                        idempotency_key=key,
                        received_at=int(row["received_at"]),
                    )
                raise DepositStoreError("deposit_idempotency_conflict") from exc
        return DepositAcceptResult(
            status="accepted",
            idempotency_key=key,
            received_at=now,
        )

    def count(self) -> int:
        with self._connect() as con:
            row = con.execute("SELECT COUNT(*) AS n FROM btc_deposit_events").fetchone()
        return int(row["n"])

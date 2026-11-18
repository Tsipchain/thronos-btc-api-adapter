"""Independent, read-only Bitcoin Core verification for deposit candidates.

The verifier does not trust node-agent confirmation claims, does not credit a
balance, and does not sign or broadcast Bitcoin transactions.  It derives all
settlement evidence from configured Bitcoin Core JSON-RPC endpoints and a
versioned local watch registry.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import secrets
import time
from typing import Any, Mapping, Protocol

import httpx

from deposit_store import MAINNET_GENESIS_HASH


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SCRIPT_HEX = re.compile(r"^(?:[0-9a-f]{2})+$")
_SATOSHIS_PER_BTC = Decimal("100000000")
_SETTLEMENT_PURPOSES = frozenset(
    {
        "bridge_pledge_eligibility",
        "bridge_deposit_accounting",
        "earn_reserve_settlement",
    }
)


class BitcoinVerificationError(RuntimeError):
    """Base class with a stable machine-readable failure code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class VerificationConflict(BitcoinVerificationError):
    """Candidate evidence conflicts with canonical chain or registry data."""


class VerificationUnavailable(BitcoinVerificationError):
    """Trusted proof could not be obtained; callers must retry without credit."""


class ReorgDetected(BitcoinVerificationError):
    """Canonical membership changed during verification."""


class BitcoinRpcError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class BitcoinRpc(Protocol):
    name: str

    def call(self, method: str, params: list[Any]) -> Any:
        ...


class BitcoinCoreRpc:
    """Authenticated Bitcoin Core JSON-RPC client with Decimal JSON parsing."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        *,
        name: str,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("bitcoin_rpc_url_invalid")
        if not username or not password or not name:
            raise ValueError("bitcoin_rpc_auth_required")
        self.name = name
        self._url = url
        self._auth = httpx.BasicAuth(username, password)
        self._client = httpx.Client(
            timeout=timeout,
            auth=self._auth,
            transport=transport,
        )

    def call(self, method: str, params: list[Any]) -> Any:
        request_id = secrets.token_hex(8)
        try:
            response = self._client.post(
                self._url,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
            )
            response.raise_for_status()
            payload = json.loads(response.text, parse_float=Decimal)
        except (httpx.HTTPError, json.JSONDecodeError, TypeError) as exc:
            raise BitcoinRpcError("bitcoin_rpc_unavailable") from exc
        if not isinstance(payload, Mapping) or payload.get("id") != request_id:
            raise BitcoinRpcError("bitcoin_rpc_invalid_response")
        error = payload.get("error")
        if error:
            code = error.get("code") if isinstance(error, Mapping) else None
            message = error.get("message") if isinstance(error, Mapping) else error
            raise BitcoinRpcError(str(message or "bitcoin_rpc_error"), code=code)
        if "result" not in payload:
            raise BitcoinRpcError("bitcoin_rpc_missing_result")
        return payload["result"]

    def close(self) -> None:
        self._client.close()


@dataclass(frozen=True)
class WatchEntry:
    address: str
    owner_ref: str
    script_pubkey_hex: str
    purpose: str


class WatchRegistry:
    """Immutable versioned mapping independently supplied to the verifier."""

    def __init__(
        self,
        *,
        version: str,
        network: str,
        genesis_hash: str,
        entries: Mapping[str, WatchEntry],
        digest: str,
    ) -> None:
        self.version = version
        self.network = network
        self.genesis_hash = genesis_hash
        self.entries = dict(entries)
        self.digest = digest

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "WatchRegistry":
        version = _text(raw.get("version"), "watch_registry_version_invalid", 128)
        network = _text(raw.get("network"), "watch_registry_network_invalid", 16)
        genesis_hash = _hash256(
            raw.get("genesis_hash"), "watch_registry_genesis_invalid"
        )
        if network != "main" or genesis_hash != MAINNET_GENESIS_HASH:
            raise VerificationConflict("watch_registry_network_identity_mismatch")
        rows = raw.get("entries")
        if not isinstance(rows, list) or not rows:
            raise VerificationConflict("watch_registry_entries_invalid")
        entries: dict[str, WatchEntry] = {}
        normalized_rows: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, Mapping) or row.get("active") is not True:
                raise VerificationConflict("watch_registry_entry_invalid")
            address = _text(row.get("address"), "watch_registry_address_invalid", 128)
            owner_ref = _text(
                row.get("owner_ref"), "watch_registry_owner_ref_invalid", 200
            )
            script_hex = str(row.get("script_pubkey_hex") or "").strip().lower()
            if not _SCRIPT_HEX.fullmatch(script_hex):
                raise VerificationConflict("watch_registry_script_invalid")
            if address in entries:
                raise VerificationConflict("watch_registry_duplicate_address")
            purpose = _text(
                row.get("purpose"), "watch_registry_purpose_invalid", 80
            )
            if purpose not in _SETTLEMENT_PURPOSES:
                raise VerificationConflict("watch_registry_purpose_invalid")
            entries[address] = WatchEntry(address, owner_ref, script_hex, purpose)
            normalized_rows.append(
                {
                    "address": address,
                    "owner_ref": owner_ref,
                    "script_pubkey_hex": script_hex,
                    "purpose": purpose,
                }
            )
        canonical = json.dumps(
            {
                "version": version,
                "network": network,
                "genesis_hash": genesis_hash,
                "entries": sorted(normalized_rows, key=lambda item: item["address"]),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            version=version,
            network=network,
            genesis_hash=genesis_hash,
            entries=entries,
            digest=hashlib.sha256(canonical.encode()).hexdigest(),
        )

    @classmethod
    def from_file(cls, path: str) -> "WatchRegistry":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VerificationUnavailable("watch_registry_unavailable") from exc
        if not isinstance(raw, Mapping):
            raise VerificationConflict("watch_registry_invalid")
        return cls.from_mapping(raw)


@dataclass(frozen=True)
class DepositCandidate:
    network: str
    genesis_hash: str
    event_id: str
    txid: str
    vout: int
    address: str
    owner_ref: str
    amount_sats: int
    block_hash: str
    block_height: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "DepositCandidate":
        network = _text(raw.get("network"), "candidate_network_invalid", 16)
        genesis = _hash256(raw.get("genesis_hash"), "candidate_genesis_invalid")
        txid = _hash256(raw.get("txid"), "candidate_txid_invalid")
        vout = _strict_int(raw.get("vout"), "candidate_vout_invalid")
        event_id = _text(raw.get("event_id"), "candidate_event_id_invalid", 160)
        if event_id != f"{network}:{txid}:{vout}":
            raise VerificationConflict("candidate_event_identity_mismatch")
        amount_sats = _strict_int(
            raw.get("amount_sats"), "candidate_amount_invalid", minimum=1
        )
        return cls(
            network=network,
            genesis_hash=genesis,
            event_id=event_id,
            txid=txid,
            vout=vout,
            address=_text(raw.get("address"), "candidate_address_invalid", 128),
            owner_ref=_text(raw.get("owner_ref"), "candidate_owner_ref_invalid", 200),
            amount_sats=amount_sats,
            block_hash=_hash256(raw.get("block_hash"), "candidate_block_hash_invalid"),
            block_height=_strict_int(
                raw.get("block_height"), "candidate_block_height_invalid"
            ),
        )


@dataclass(frozen=True)
class CanonicalDepositProof:
    proof_ref: str
    event_id: str
    network: str
    txid: str
    vout: int
    address: str
    owner_ref: str
    amount_sats: int
    script_pubkey_hex: str
    block_hash: str
    block_height: int
    confirmations: int
    tip_height: int
    tip_hash: str
    registry_version: str
    registry_digest: str
    purpose: str
    verifier_source: str
    verified_at: int

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


class IndependentBitcoinVerifier:
    """Derive canonical deposit proof without trusting receipt finality fields."""

    def __init__(
        self,
        primary: BitcoinRpc,
        registry: WatchRegistry,
        *,
        archival: BitcoinRpc | None = None,
        minimum_confirmations: int = 6,
        now_fn=time.time,
    ) -> None:
        if minimum_confirmations < 6:
            raise ValueError("minimum_confirmations_below_six")
        self.primary = primary
        self.archival = archival
        self.registry = registry
        self.minimum_confirmations = minimum_confirmations
        self.now_fn = now_fn

    def verify(self, candidate: DepositCandidate) -> CanonicalDepositProof:
        self._verify_network_identity(self.primary)
        if (
            candidate.network != "main"
            or candidate.genesis_hash != MAINNET_GENESIS_HASH
            or self.registry.network != candidate.network
            or self.registry.genesis_hash != candidate.genesis_hash
        ):
            raise VerificationConflict("candidate_network_identity_mismatch")

        watched = self.registry.entries.get(candidate.address)
        if watched is None:
            raise VerificationConflict("candidate_address_not_watched")
        if watched.owner_ref != candidate.owner_ref:
            raise VerificationConflict("candidate_recipient_mismatch")

        canonical_hash = _hash256(
            self._primary_call("getblockhash", [candidate.block_height]),
            "canonical_block_hash_invalid",
        )
        if canonical_hash != candidate.block_hash:
            raise VerificationConflict("canonical_block_mismatch")

        tip_height = _strict_int(
            self._primary_call("getblockcount", []), "canonical_tip_invalid"
        )
        confirmations = tip_height - candidate.block_height + 1
        if confirmations < self.minimum_confirmations:
            raise VerificationConflict("insufficient_confirmations")

        tx, source = self._historical_transaction(candidate)
        if not isinstance(tx, Mapping):
            raise VerificationConflict("transaction_proof_invalid")
        if _hash256(tx.get("txid"), "transaction_txid_invalid") != candidate.txid:
            raise VerificationConflict("transaction_txid_mismatch")
        if _hash256(tx.get("blockhash"), "transaction_block_hash_invalid") != canonical_hash:
            raise VerificationConflict("transaction_not_in_claimed_block")

        outputs = tx.get("vout")
        if not isinstance(outputs, list):
            raise VerificationConflict("transaction_outputs_invalid")
        matches = [
            item for item in outputs
            if isinstance(item, Mapping) and item.get("n") == candidate.vout
        ]
        if len(matches) != 1:
            raise VerificationConflict("candidate_vout_not_found")
        output = matches[0]
        script = output.get("scriptPubKey")
        if not isinstance(script, Mapping):
            raise VerificationConflict("candidate_script_pubkey_invalid")
        script_hex = str(script.get("hex") or "").strip().lower()
        if script_hex != watched.script_pubkey_hex:
            raise VerificationConflict("candidate_script_pubkey_mismatch")
        if _script_address(script) != candidate.address:
            raise VerificationConflict("candidate_destination_mismatch")
        if _btc_to_sats(output.get("value")) != candidate.amount_sats:
            raise VerificationConflict("candidate_amount_mismatch")

        # Recheck canonical membership and depth immediately before producing
        # settlement-eligibility evidence. A change is a reorg, not a retryable
        # success based on stale observations.
        final_hash = _hash256(
            self._primary_call("getblockhash", [candidate.block_height]),
            "canonical_block_hash_invalid",
        )
        if final_hash != canonical_hash:
            raise ReorgDetected("reorg_detected_before_handoff")
        final_tip = _strict_int(
            self._primary_call("getblockcount", []), "canonical_tip_invalid"
        )
        final_confirmations = final_tip - candidate.block_height + 1
        if final_confirmations < self.minimum_confirmations:
            raise ReorgDetected("confirmation_depth_changed_before_handoff")
        tip_hash = _hash256(
            self._primary_call("getblockhash", [final_tip]),
            "canonical_tip_hash_invalid",
        )

        proof_payload = {
            "event_id": candidate.event_id,
            "network": candidate.network,
            "txid": candidate.txid,
            "vout": candidate.vout,
            "address": candidate.address,
            "owner_ref": candidate.owner_ref,
            "amount_sats": candidate.amount_sats,
            "script_pubkey_hex": script_hex,
            "block_hash": canonical_hash,
            "block_height": candidate.block_height,
            "confirmations": final_confirmations,
            "tip_height": final_tip,
            "tip_hash": tip_hash,
            "registry_version": self.registry.version,
            "registry_digest": self.registry.digest,
            "purpose": watched.purpose,
            "verifier_source": source,
        }
        canonical = json.dumps(proof_payload, sort_keys=True, separators=(",", ":"))
        return CanonicalDepositProof(
            proof_ref="btcp_" + hashlib.sha256(canonical.encode()).hexdigest(),
            verified_at=int(self.now_fn()),
            **proof_payload,
        )

    def _primary_call(self, method: str, params: list[Any]) -> Any:
        try:
            return self.primary.call(method, params)
        except BitcoinRpcError as exc:
            raise VerificationUnavailable("primary_bitcoin_core_unavailable") from exc

    def _verify_network_identity(self, rpc: BitcoinRpc) -> None:
        try:
            info = rpc.call("getblockchaininfo", [])
            genesis = rpc.call("getblockhash", [0])
        except BitcoinRpcError as exc:
            raise VerificationUnavailable("bitcoin_core_identity_unavailable") from exc
        if not isinstance(info, Mapping) or info.get("chain") != "main":
            raise VerificationConflict("bitcoin_core_network_mismatch")
        if _hash256(genesis, "bitcoin_core_genesis_invalid") != MAINNET_GENESIS_HASH:
            raise VerificationConflict("bitcoin_core_genesis_mismatch")

    def _historical_transaction(
        self, candidate: DepositCandidate
    ) -> tuple[Mapping[str, Any], str]:
        try:
            result = self.primary.call(
                "getrawtransaction", [candidate.txid, True, candidate.block_hash]
            )
            return result, self.primary.name
        except BitcoinRpcError as exc:
            if not _historical_data_unavailable(exc):
                raise VerificationUnavailable("primary_transaction_proof_unavailable") from exc
        if self.archival is None:
            raise VerificationUnavailable("archival_transaction_proof_required")
        self._verify_network_identity(self.archival)
        try:
            result = self.archival.call(
                "getrawtransaction", [candidate.txid, True, candidate.block_hash]
            )
        except BitcoinRpcError as exc:
            raise VerificationUnavailable("archival_transaction_proof_unavailable") from exc
        return result, self.archival.name


def _historical_data_unavailable(exc: BitcoinRpcError) -> bool:
    message = str(exc).lower()
    return exc.code in (-5, -1) or any(
        token in message for token in ("pruned", "txindex", "not found", "not available")
    )


def _hash256(value: Any, code: str) -> str:
    result = str(value or "").strip().lower()
    if not _HEX64.fullmatch(result):
        raise VerificationConflict(code)
    return result


def _text(value: Any, code: str, limit: int) -> str:
    result = str(value or "").strip()
    if not result or len(result) > limit:
        raise VerificationConflict(code)
    return result


def _strict_int(value: Any, code: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise VerificationConflict(code)
    return value


def _script_address(script: Mapping[str, Any]) -> str:
    direct = str(script.get("address") or "").strip()
    if direct:
        return direct
    addresses = script.get("addresses")
    if isinstance(addresses, list) and len(addresses) == 1:
        return str(addresses[0] or "").strip()
    return ""


def _btc_to_sats(value: Any) -> int:
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
        sats = amount * _SATOSHIS_PER_BTC
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise VerificationConflict("candidate_amount_invalid") from exc
    if not amount.is_finite() or amount <= 0 or sats != sats.to_integral_value():
        raise VerificationConflict("candidate_amount_invalid")
    return int(sats)

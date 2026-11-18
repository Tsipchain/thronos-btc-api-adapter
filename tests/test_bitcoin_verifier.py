from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import json

import httpx
import pytest

from bitcoin_verifier import (
    BitcoinCoreRpc,
    BitcoinRpcError,
    DepositCandidate,
    IndependentBitcoinVerifier,
    ReorgDetected,
    VerificationConflict,
    VerificationUnavailable,
    WatchRegistry,
)
from deposit_store import MAINNET_GENESIS_HASH


TXID = "11" * 32
BLOCK_HASH = "aa" * 32
TIP_HASH = "bb" * 32
REORG_HASH = "cc" * 32
ADDRESS = "bc1qindependentverifier000000000000000000000"
OWNER_REF = "bridge-intent-42"
SCRIPT_HEX = "0014" + "12" * 20
HEIGHT = 100
TIP = 105


class FakeRpc:
    def __init__(self, *, name="primary", chain="main", genesis=MAINNET_GENESIS_HASH):
        self.name = name
        self.chain = chain
        self.genesis = genesis
        self.tip = TIP
        self.block_hashes = {HEIGHT: BLOCK_HASH, TIP: TIP_HASH}
        self.transaction = transaction()
        self.transaction_error = None
        self.height_sequences = defaultdict(list)
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "getblockchaininfo":
            return {"chain": self.chain, "pruned": False}
        if method == "getblockcount":
            return self.tip
        if method == "getblockhash":
            height = params[0]
            if height == 0:
                return self.genesis
            sequence = self.height_sequences[height]
            if sequence:
                return sequence.pop(0)
            return self.block_hashes[height]
        if method == "getrawtransaction":
            if self.transaction_error is not None:
                raise self.transaction_error
            return self.transaction
        raise AssertionError(f"unexpected RPC method {method}")


def registry(*, owner_ref=OWNER_REF, script_hex=SCRIPT_HEX, version="registry-v7"):
    return WatchRegistry.from_mapping(
        {
            "version": version,
            "network": "main",
            "genesis_hash": MAINNET_GENESIS_HASH,
            "entries": [
                {
                    "address": ADDRESS,
                    "owner_ref": owner_ref,
                    "script_pubkey_hex": script_hex,
                    "active": True,
                }
            ],
        }
    )


def candidate(**changes):
    raw = {
        "network": "main",
        "genesis_hash": MAINNET_GENESIS_HASH,
        "event_id": f"main:{TXID}:0",
        "txid": TXID,
        "vout": 0,
        "address": ADDRESS,
        "owner_ref": OWNER_REF,
        "amount_sats": 12_345,
        "block_hash": BLOCK_HASH,
        "block_height": HEIGHT,
    }
    raw.update(changes)
    if "vout" in changes and "event_id" not in changes:
        raw["event_id"] = f"main:{TXID}:{changes['vout']}"
    return DepositCandidate.from_mapping(raw)


def transaction(**output_changes):
    output = {
        "n": 0,
        "value": Decimal("0.00012345"),
        "scriptPubKey": {
            "hex": SCRIPT_HEX,
            "address": ADDRESS,
            "type": "witness_v0_keyhash",
        },
    }
    output.update(output_changes)
    return {"txid": TXID, "blockhash": BLOCK_HASH, "vout": [output]}


def verifier(primary=None, *, archival=None, watch_registry=None):
    return IndependentBitcoinVerifier(
        primary or FakeRpc(),
        watch_registry or registry(),
        archival=archival,
        minimum_confirmations=6,
        now_fn=lambda: 1_800_000_000,
    )


def test_valid_deposit_produces_deterministic_canonical_proof():
    proof = verifier().verify(candidate())

    assert proof.event_id == f"main:{TXID}:0"
    assert proof.amount_sats == 12_345
    assert proof.confirmations == 6
    assert proof.registry_version == "registry-v7"
    assert proof.verifier_source == "primary"
    assert proof.verified_at == 1_800_000_000
    assert proof.proof_ref.startswith("btcp_")
    assert proof == verifier().verify(candidate())


@pytest.mark.parametrize(
    ("chain", "genesis", "error"),
    [
        ("test", MAINNET_GENESIS_HASH, "bitcoin_core_network_mismatch"),
        ("main", "22" * 32, "bitcoin_core_genesis_mismatch"),
    ],
)
def test_wrong_bitcoin_core_network_or_genesis_fails_closed(chain, genesis, error):
    with pytest.raises(VerificationConflict, match=error):
        verifier(FakeRpc(chain=chain, genesis=genesis)).verify(candidate())


def test_candidate_network_identity_mismatch_fails_closed():
    raw = candidate().__dict__ | {"network": "test", "event_id": f"test:{TXID}:0"}
    with pytest.raises(VerificationConflict, match="candidate_network_identity_mismatch"):
        verifier().verify(DepositCandidate.from_mapping(raw))


@pytest.mark.parametrize(
    ("tx_change", "candidate_change", "error"),
    [
        ({"n": 1}, {}, "candidate_vout_not_found"),
        ({"scriptPubKey": {"hex": SCRIPT_HEX, "address": "bc1qwrong"}}, {},
         "candidate_destination_mismatch"),
        ({"scriptPubKey": {"hex": "0014" + "34" * 20, "address": ADDRESS}}, {},
         "candidate_script_pubkey_mismatch"),
        ({"value": Decimal("0.00012346")}, {}, "candidate_amount_mismatch"),
        ({"value": Decimal("0.000000001")}, {}, "candidate_amount_invalid"),
    ],
)
def test_wrong_output_evidence_fails_closed(tx_change, candidate_change, error):
    rpc = FakeRpc()
    rpc.transaction = transaction(**tx_change)
    with pytest.raises(VerificationConflict, match=error):
        verifier(rpc).verify(candidate(**candidate_change))


def test_wrong_vout_fails_closed():
    with pytest.raises(VerificationConflict, match="candidate_vout_not_found"):
        verifier().verify(candidate(vout=3))


def test_wrong_registry_recipient_fails_closed():
    with pytest.raises(VerificationConflict, match="candidate_recipient_mismatch"):
        verifier(watch_registry=registry(owner_ref="another-intent")).verify(candidate())


def test_insufficient_independently_computed_confirmations_fails_closed():
    rpc = FakeRpc()
    rpc.tip = HEIGHT + 4
    with pytest.raises(VerificationConflict, match="insufficient_confirmations"):
        verifier(rpc).verify(candidate())


def test_canonical_block_mismatch_fails_closed():
    rpc = FakeRpc()
    rpc.block_hashes[HEIGHT] = REORG_HASH
    with pytest.raises(VerificationConflict, match="canonical_block_mismatch"):
        verifier(rpc).verify(candidate())


def test_pruned_history_without_archival_core_remains_pending():
    rpc = FakeRpc()
    rpc.transaction_error = BitcoinRpcError("Block not available (pruned data)", code=-1)
    with pytest.raises(VerificationUnavailable, match="archival_transaction_proof_required"):
        verifier(rpc).verify(candidate())


def test_authenticated_archival_core_can_supply_pruned_history():
    primary = FakeRpc()
    primary.transaction_error = BitcoinRpcError("No such mempool transaction", code=-5)
    archival = FakeRpc(name="archival-core")

    proof = verifier(primary, archival=archival).verify(candidate())

    assert proof.verifier_source == "archival-core"
    assert ("getblockchaininfo", []) in archival.calls
    assert ("getblockhash", [0]) in archival.calls
    assert ("getrawtransaction", [TXID, True, BLOCK_HASH]) in archival.calls


def test_archival_core_network_mismatch_fails_closed():
    primary = FakeRpc()
    primary.transaction_error = BitcoinRpcError("pruned", code=-1)
    archival = FakeRpc(name="archive", chain="test")
    with pytest.raises(VerificationConflict, match="bitcoin_core_network_mismatch"):
        verifier(primary, archival=archival).verify(candidate())


def test_reorg_detected_by_immediate_pre_handoff_recheck():
    rpc = FakeRpc()
    rpc.height_sequences[HEIGHT] = [BLOCK_HASH, REORG_HASH]
    with pytest.raises(ReorgDetected, match="reorg_detected_before_handoff"):
        verifier(rpc).verify(candidate())


def test_core_rpc_requires_auth_and_preserves_decimal_satoshi_precision():
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("authorization")
        body = json.loads(request.content)
        return httpx.Response(
            200,
            text=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"value": 0.00012345},
                    "error": None,
                }
            ),
        )

    rpc = BitcoinCoreRpc(
        "https://archive.internal.invalid",
        "verifier",
        "not-logged",
        name="archive",
        transport=httpx.MockTransport(handler),
    )
    result = rpc.call("fixture", [])
    rpc.close()

    assert observed["authorization"].startswith("Basic ")
    assert result["value"] == Decimal("0.00012345")

"""MM-R1.5 operator-gated, clean-room ``OrderFilled`` capture.

This module turns Polymarket CTF Exchange V2 ``OrderFilled`` logs into pinned,
provenance-bearing :class:`~veridex.maker.trade_artifact.TradeArtifact` bundles.
It is split into three responsibilities, none of which touch the network at
import or test time:

* :func:`decode_order_filled` — a **clean-room** pure decoder. It was written from
  the CTF Exchange V2 ``OrderFilled`` event ABI (field names + 6-decimal USDC
  scaling), NOT copied from any GPL-licensed reference implementation. It maps one
  decoded log dict to a single :class:`NormalizedTradeRow`, deriving a native
  ``[0, 1]`` ``price = usdc_leg / share_leg`` and rejecting any out-of-range price.
* :func:`build_trade_artifact` — assembles a validated ``TradeArtifact`` offline
  from already-decoded rows (dedup + cp1 reconciliation). It receives **no**
  operator token and writes none into the manifest.
* :func:`capture_order_filled_artifact` — the operator entrypoint. It reads the
  ``HYPERSYNC_API`` operator secret from the environment **only** to gate the run
  (fail-closed when absent and no client is injected) and never writes it into any
  artifact / manifest / log / return value. The log source is an injected /
  overridable client, so tests exercise the fail-closed path with no network.

Clean-room attestation: the decoder arithmetic below is derived solely from the
public event ABI; no code was copied from any GPL-licensed reference, and this
module imports only the standard library and ``veridex.*``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Protocol

from veridex.maker.mapping import PINNED_MAPPING_HASH
from veridex.maker.markout import assert_native_prob
from veridex.maker.trades import AggressorSide
from veridex.maker.trade_artifact import (
    PINNED_CHAIN_ID,
    PINNED_CLEANROOM_ATTESTATION,
    PINNED_CONTRACT_ADDRESS,
    PINNED_EVENT_SIGNATURE,
    PINNED_FIXTURE_COUNT,
    PINNED_SIDE_COUNT,
    PINNED_SOURCE,
    NormalizedTradeRow,
    TradeArtifact,
    dedup_normalized_rows,
    recompute_artifact_hash,
)

__all__ = [
    "OrderFilledLogSource",
    "build_trade_artifact",
    "capture_order_filled_artifact",
    "decode_order_filled",
]


class OrderFilledLogSource(Protocol):
    """An injected, network-owning source of decoded ``OrderFilled`` logs.

    The operator supplies the concrete implementation (e.g. a thin HyperSync
    adapter that already holds the operator token). Keeping the network behind this
    Protocol is what lets :mod:`veridex.maker.capture` import only stdlib +
    ``veridex.*`` and lets tests inject a network-free fake.
    """

    def fetch_order_filled_logs(
        self, *, from_block: int, to_block: int
    ) -> list[dict[str, Any]]:
        """Return decoded ``OrderFilled`` log dicts for the block range."""
        ...


#: The CTF Exchange V2 collateral (USDC) leg is emitted with assetId ``"0"``.
_COLLATERAL_ASSET_ID = "0"

#: USDC and CTF outcome tokens are both 6-decimal; scaling cancels in the price
#: ratio but is applied to recover a human-scale ``size`` (shares).
_AMOUNT_SCALE = 1_000_000


def decode_order_filled(log: dict[str, Any]) -> NormalizedTradeRow:
    """Clean-room decode of one ``OrderFilled`` log into a normalized trade row.

    The CTF Exchange V2 ``OrderFilled`` event pairs a USDC (collateral) leg with an
    outcome-token (share) leg. Exactly one of ``makerAssetId`` / ``takerAssetId`` is
    the collateral asset (id ``"0"``); the other is the traded outcome token. The
    native price is ``usdc_leg / share_leg`` (both 6-decimal, so the scale cancels),
    ``size`` is the share leg in human units, and the aggressor (taker) side is derived
    from the collateral leg: a maker who supplied collateral BOUGHT shares, so the
    aggressor SOLD. The aggressor is NEVER derived from an ABI-external ``side`` key.

    Args:
        log: A decoded ``OrderFilled`` log with keys ``block_number,
            transaction_hash, log_index, block_timestamp, maker, taker,
            makerAssetId, takerAssetId, makerAmountFilled, takerAmountFilled``. An
            optional ``side`` key is NOT trusted for the aggressor sign; when present
            it is only cross-checked against the leg derivation.

    Returns:
        The decoded :class:`NormalizedTradeRow` (never a Veridex fill).

    Raises:
        ValueError: If neither / both legs are the collateral asset, the share leg is
            zero, or a present ``side`` key disagrees with the collateral-leg-derived
            maker side.
        MarkoutError: If the derived ``price`` is outside ``[0, 1]``.
    """
    maker_asset_id = str(log["makerAssetId"])
    taker_asset_id = str(log["takerAssetId"])
    maker_amount = int(log["makerAmountFilled"])
    taker_amount = int(log["takerAmountFilled"])

    maker_is_collateral = maker_asset_id == _COLLATERAL_ASSET_ID
    taker_is_collateral = taker_asset_id == _COLLATERAL_ASSET_ID
    if maker_is_collateral == taker_is_collateral:
        raise ValueError(
            "OrderFilled log must have exactly one collateral (assetId '0') leg; "
            f"got makerAssetId={maker_asset_id!r}, takerAssetId={taker_asset_id!r}"
        )

    if maker_is_collateral:
        usdc_amount, share_amount, token_id = maker_amount, taker_amount, taker_asset_id
    else:
        usdc_amount, share_amount, token_id = taker_amount, maker_amount, maker_asset_id

    if share_amount == 0:
        raise ValueError("OrderFilled share leg is zero; price undefined")

    price = usdc_amount / share_amount
    assert_native_prob(price, "price")
    size = share_amount / _AMOUNT_SCALE

    # Derive the maker's side from the COLLATERAL LEG the decoder already computed, NOT from
    # any ABI-external ``side`` key: the pinned OrderFilled(bytes32,address,address,uint256,
    # uint256,uint256,uint256,uint256) has NO ``side`` param, so an operator adapter that
    # synthesizes one and maps the taker's side there would silently flip every aggressor sign
    # (negating signed_flow / post_trade_fv_markout / picked_off / toxic-vs-benign). A maker who
    # SUPPLIED collateral (assetId "0") BOUGHT shares, so the aggressor (taker) SOLD.
    maker_buys = maker_is_collateral
    # Defense-in-depth: if a ``side`` key IS present, it must AGREE with the leg derivation
    # (maker BUY=0 / SELL=1); disagreement means the external key is untrustworthy -> raise.
    if "side" in log:
        side_says_maker_buys = int(log["side"]) == 0
        if side_says_maker_buys != maker_buys:
            raise ValueError(
                "OrderFilled 'side' disagrees with the collateral-leg-derived maker side "
                f"(side={log['side']!r} => maker_buys={side_says_maker_buys}, but "
                f"maker_is_collateral={maker_is_collateral} => maker_buys={maker_buys}); "
                "refusing to trust the ABI-external 'side' key"
            )
    aggressor_side = AggressorSide.SELL if maker_buys else AggressorSide.BUY

    return NormalizedTradeRow(
        ts=int(log["block_timestamp"]),
        price=price,
        size=size,
        aggressor_side=aggressor_side,
        condition_id=str(log.get("condition_id", "")),
        token_id=token_id,
        block_number=int(log["block_number"]),
        tx_hash=str(log["transaction_hash"]),
        log_index=int(log["log_index"]),
    )


def build_trade_artifact(
    rows: list[NormalizedTradeRow],
    *,
    records: list[dict[str, Any]],
    manifest_meta: dict[str, Any],
) -> TradeArtifact:
    """Assemble a validated :class:`TradeArtifact` offline from decoded rows.

    The build is fully offline and receives **no** operator token: dedup collapses
    rows sharing a chain-event key, the remaining rows are reconciled against the
    pinned cp1 records (matched vs. unmatched by ``token_id``), the artifact hash is
    recomputed over the surviving rows, and the pinned mapping hash is stamped. The
    counts satisfy the ``TradeArtifact`` reconciliation invariant
    (``rows_decoded == matched + unmatched + malformed + duplicate_dropped``).

    Args:
        rows: Decoded normalized rows (may contain duplicate event keys).
        records: The pinned cp1 mapping records; a row is ``matched_cp1`` when its
            ``token_id`` appears in these records.
        manifest_meta: The descriptive manifest fields (schema/decoder/source/block
            range/provider/attestation, etc.). Must carry **no** operator secret;
            the count / hash / mapping-pin / rows fields are computed here and must
            not be supplied.

    Returns:
        The validated :class:`TradeArtifact`.

    Raises:
        ValueError: If ``manifest_meta`` supplies a field computed here.
    """
    computed_keys = {
        "artifact_hash",
        "rows_decoded",
        "rows_matched_cp1",
        "rows_unmatched",
        "rows_malformed",
        "rows_duplicate_dropped",
        "mapping_content_hash",
        "rows",
    }
    supplied_conflicts = computed_keys & manifest_meta.keys()
    if supplied_conflicts:
        raise ValueError(
            f"manifest_meta must not supply computed fields: {sorted(supplied_conflicts)}"
        )

    rows_decoded = len(rows)
    unique_rows, duplicate_dropped = dedup_normalized_rows(rows)

    # The clean-room decoder emits ``condition_id=""`` (the OrderFilled ABI carries no
    # condition_id). Enrich each cp1-matched row's ``condition_id`` from the pinned mapping
    # (indexed by the unique ``token_id``) so the artifact rows carry the real market
    # identity and the runner's ``(condition_id, token_id)`` real-artifact join matches on
    # real data (AC-102). Enrichment belongs in build (which holds the mapping), keeping the
    # decoder pure; unmatched rows keep ``condition_id=""``. Rows are frozen -> model_copy.
    cp1_token_ids = {str(record["token_id"]) for record in records}
    cp1_condition_by_token = {
        str(record["token_id"]): str(record.get("condition_id", "") or "")
        for record in records
    }
    unique_rows = [
        row.model_copy(update={"condition_id": cp1_condition_by_token[row.token_id]})
        if cp1_condition_by_token.get(row.token_id)
        else row
        for row in unique_rows
    ]

    matched = [row for row in unique_rows if row.token_id in cp1_token_ids]
    unmatched = [row for row in unique_rows if row.token_id not in cp1_token_ids]

    manifest = dict(manifest_meta)
    manifest.update(
        # Stamp the claim-bearing provenance from the SINGLE SOURCE OF TRUTH constants the
        # TradeArtifact validator pins to (capture ↔ artifact consistency): a real capture
        # ALWAYS produces an admissible artifact, and the offline assembler cannot be
        # tricked into emitting synthetic provenance (source/chain/contract/event-sig/
        # counts/clean-room) around real rows. `token_supplied_externally=True` because a
        # real capture is operator-gated by an externally supplied token (never committed).
        source=PINNED_SOURCE,
        chain_id=PINNED_CHAIN_ID,
        contract_address=PINNED_CONTRACT_ADDRESS,
        event_signature=PINNED_EVENT_SIGNATURE,
        token_supplied_externally=True,
        fixture_count=PINNED_FIXTURE_COUNT,
        side_count=PINNED_SIDE_COUNT,
        cleanroom_attestation=PINNED_CLEANROOM_ATTESTATION,
        artifact_hash=recompute_artifact_hash(list(unique_rows)),
        rows_decoded=rows_decoded,
        rows_matched_cp1=len(matched),
        rows_unmatched=len(unmatched),
        rows_malformed=0,
        rows_duplicate_dropped=duplicate_dropped,
        mapping_content_hash=PINNED_MAPPING_HASH,
        rows=tuple(unique_rows),
    )
    return TradeArtifact(**manifest)


def capture_order_filled_artifact(
    *,
    from_block: int,
    to_block: int,
    out_path: str | Path,
    client: OrderFilledLogSource | None = None,
    records: list[dict[str, Any]] | None = None,
    manifest_meta: dict[str, Any] | None = None,
) -> TradeArtifact:
    """Operator entrypoint: capture ``OrderFilled`` logs into a pinned artifact.

    This is the ONLY place the ``HYPERSYNC_API`` operator secret is read, and it is
    read purely to **gate** the run: the token is never passed into
    :func:`build_trade_artifact`, never written into the artifact/manifest, and
    never returned. The log source is an injected :class:`OrderFilledLogSource`, so
    this function performs no network import itself and tests exercise it with a
    network-free fake.

    Fail-closed contract: with no injected ``client`` and no ``HYPERSYNC_API`` in the
    environment, this raises before any network or file I/O.

    Args:
        from_block: First block (inclusive) of the capture range.
        to_block: Last block (inclusive) of the capture range.
        out_path: Destination path for the JSON artifact.
        client: Injected log source. When ``None``, a token must be present AND a
            client must still be provided (this module builds no network client).
        records: Pinned cp1 mapping records; loaded from the pinned mapping artifact
            when ``None``.
        manifest_meta: Descriptive manifest fields; a default operator manifest is
            built when ``None``.

    Returns:
        The validated, persisted :class:`TradeArtifact`.

    Raises:
        RuntimeError: If neither an injected client nor the ``HYPERSYNC_API`` token
            is available (fail-closed), performing no network or file I/O.
    """
    token = os.environ.get("HYPERSYNC_API")
    if client is None:
        if not token:
            raise RuntimeError(
                "capture fails closed: the operator capture token is not set and no "
                "log-source client was injected; no network call or file write was "
                "performed (see docs/maker/r15-capture-runbook.md)"
            )
        raise RuntimeError(
            "no OrderFilledLogSource injected: build a HyperSync-backed client from "
            "the operator capture token in the operator harness and pass it as "
            "client=...; veridex.maker.capture performs no network import of its own"
        )

    logs = client.fetch_order_filled_logs(from_block=from_block, to_block=to_block)
    rows = [decode_order_filled(log) for log in logs]

    if records is None:
        records = _load_pinned_cp1_records()
    if manifest_meta is None:
        manifest_meta = _default_operator_manifest_meta(
            from_block=from_block,
            to_block=to_block,
            token_supplied_externally=token is not None,
        )

    artifact = build_trade_artifact(rows, records=records, manifest_meta=manifest_meta)
    Path(out_path).write_text(
        json.dumps(artifact.model_dump(mode="json"), sort_keys=True, indent=2)
    )
    return artifact


def _load_pinned_cp1_records() -> list[dict[str, Any]]:
    """Load the pinned cp1 token set from committed mapping bytes (no network)."""
    from veridex.maker.mapping import DEFAULT_MAPPING_PATH, load_resolved_market_lookup

    parsed, _hash = load_resolved_market_lookup(DEFAULT_MAPPING_PATH)
    # ``condition_id`` rides along so ``build_trade_artifact`` can enrich matched rows'
    # market identity (the decoder leaves ``condition_id=""``); it is public mapping data.
    return [
        {"token_id": record.token_id, "condition_id": record.condition_id}
        for record in parsed
    ]


def _default_operator_manifest_meta(
    *, from_block: int, to_block: int, token_supplied_externally: bool
) -> dict[str, Any]:
    """Build a default descriptive manifest (carries no operator secret)."""
    return {
        "raw_artifact_hash": None,
        "schema_version": "v1",
        "decoder_version": "orderfilled-cleanroom-v1",
        "decoder_commit": None,
        # Provenance sourced from the pinned constants the validator enforces; the same
        # values are re-stamped in build_trade_artifact, so capture and validator can never
        # disagree. (`token_supplied_externally` is recorded here for the descriptive
        # manifest; build_trade_artifact force-stamps it True on the admissible artifact.)
        "source": PINNED_SOURCE,
        "chain_id": PINNED_CHAIN_ID,
        "contract_address": PINNED_CONTRACT_ADDRESS,
        "event_signature": PINNED_EVENT_SIGNATURE,
        "from_block": from_block,
        "to_block": to_block,
        "reorg_buffer_confs": 20,
        # Real wall-clock capture time on the live operator path. This is manifest metadata
        # only -- it does NOT feed ``artifact_hash`` (that is recomputed over the trade ROWS
        # alone in build_trade_artifact), so hashed artifact bytes stay deterministic. Tests
        # pass an explicit ``capture_ts`` for determinism instead of calling this default.
        "capture_ts": int(time.time()),
        "capture_tool_id": "veridex-maker-capture",
        "provider_id": "hypersync",
        "token_supplied_externally": token_supplied_externally,
        "fixture_count": PINNED_FIXTURE_COUNT,
        "side_count": PINNED_SIDE_COUNT,
        "cleanroom_attestation": PINNED_CLEANROOM_ATTESTATION,
    }

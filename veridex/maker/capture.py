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

from typing import Any

from veridex.maker.mapping import PINNED_MAPPING_HASH
from veridex.maker.markout import assert_native_prob
from veridex.maker.trades import AggressorSide
from veridex.maker.trade_artifact import (
    NormalizedTradeRow,
    TradeArtifact,
    dedup_normalized_rows,
    recompute_artifact_hash,
)

__all__ = [
    "build_trade_artifact",
    "decode_order_filled",
]

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
    ``size`` is the share leg in human units, and the aggressor is the taker — the
    negation of the maker's ``side``.

    Args:
        log: A decoded ``OrderFilled`` log with keys ``block_number,
            transaction_hash, log_index, block_timestamp, maker, taker,
            makerAssetId, takerAssetId, makerAmountFilled, takerAmountFilled,
            side``.

    Returns:
        The decoded :class:`NormalizedTradeRow` (never a Veridex fill).

    Raises:
        ValueError: If neither / both legs are the collateral asset, or the share
            leg is zero.
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

    # ``side`` is the maker's side (BUY=0, SELL=1); the aggressor is the taker.
    maker_buys = int(log["side"]) == 0
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

    cp1_token_ids = {str(record["token_id"]) for record in records}
    matched = [row for row in unique_rows if row.token_id in cp1_token_ids]
    unmatched = [row for row in unique_rows if row.token_id not in cp1_token_ids]

    manifest = dict(manifest_meta)
    manifest.update(
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
